import os
import re
import json
import hashlib
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

from flask import Flask, request, jsonify, send_from_directory, session, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from werkzeug.middleware.proxy_fix import ProxyFix

# -------------------------
# App / Config
# -------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder='static', template_folder='templates')

# Para Railway (HTTPS atrás de proxy) - ajuda cookie de sessão ficar consistente
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# IMPORTANTE: em produção, defina SECRET_KEY nas Variables do Railway
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key-change-me')

# Cookie de sessão mais “padrão” para PWA
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# Railway geralmente roda em https; se você estiver acessando https, isso é ok.
# Se estiver testando http local, pode setar False localmente.
app.config['SESSION_COOKIE_SECURE'] = (os.getenv("COOKIE_SECURE", "1") == "1")

# Railway fornece DATABASE_URL
_raw_db_url = (os.getenv('DATABASE_URL', '') or '').strip()
if _raw_db_url.startswith('postgres://'):
    _raw_db_url = _raw_db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = _raw_db_url or 'sqlite:///' + os.path.join(BASE_DIR, 'local.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Pool pequeno para Postgres Free
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 280,
    'pool_size': int(os.getenv('DB_POOL_SIZE', '3')),
    'max_overflow': int(os.getenv('DB_MAX_OVERFLOW', '2')),
}

DB_ENABLED = bool(_raw_db_url)

# -------------------------
# DB
# -------------------------
db = SQLAlchemy(app)


class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(64), nullable=False)

    # Pode existir ou não na sua tabela - vamos garantir por migração leve
    password_set = db.Column(db.Boolean, nullable=False, server_default=text('false'))

    # Campos extras (se já existirem no banco, ótimo; se não, não atrapalha)
    nome_apelido = db.Column(db.String(120), nullable=True)
    nome_completo = db.Column(db.String(255), nullable=True)
    telefone = db.Column(db.String(40), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Transaction(db.Model):
    __tablename__ = 'transactions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    tipo = db.Column(db.String(16), nullable=False)  # RECEITA | GASTO

    # ✅ SUA TABELA NO POSTGRES USA "data"
    data = db.Column(db.Date, nullable=False, index=True)

    categoria = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    valor = db.Column(db.Float, nullable=False)

    origem = db.Column(db.String(16), nullable=False, default='APP')  # APP | WA
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class WaLink(db.Model):
    """
    Pelo seu print, sua tabela wa_links tem:
      id, wa_number, user_id, created_at, wa_from, user_email
    Vamos mapear isso para evitar 500 se o webhook usar.
    """
    __tablename__ = 'wa_links'

    id = db.Column(db.Integer, primary_key=True)
    wa_number = db.Column(db.String(40), nullable=True)
    user_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    wa_from = db.Column(db.String(40), nullable=True, index=True)
    user_email = db.Column(db.String(255), nullable=True)


class ProcessedMessage(db.Model):
    __tablename__ = 'processed_messages'

    id = db.Column(db.Integer, primary_key=True)
    msg_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    wa_from = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


def _create_tables_if_needed() -> None:
    """Create tables (simple approach: no migrations). + migração leve idempotente."""
    try:
        db.create_all()

        def _add_col_if_missing(table: str, ddl: str):
            try:
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {ddl}"))
                db.session.commit()
            except Exception:
                db.session.rollback()

        insp = inspect(db.engine)
        tables = set(insp.get_table_names())

        if 'users' in tables:
            _add_col_if_missing('users', 'password_set BOOLEAN NOT NULL DEFAULT false')
            _add_col_if_missing('users', 'nome_apelido VARCHAR(120)')
            _add_col_if_missing('users', 'nome_completo VARCHAR(255)')
            _add_col_if_missing('users', 'telefone VARCHAR(40)')

        if 'transactions' in tables:
            # garante coluna "data" (a sua já existe; isso é só para evitar ambientes novos/antigos)
            _add_col_if_missing('transactions', 'data DATE')
            _add_col_if_missing('transactions', 'origem VARCHAR(16)')
            _add_col_if_missing('transactions', 'created_at TIMESTAMP')

        if 'processed_messages' in tables:
            _add_col_if_missing('processed_messages', 'wa_from VARCHAR(40)')

        if 'wa_links' in tables:
            _add_col_if_missing('wa_links', 'wa_from VARCHAR(40)')
            _add_col_if_missing('wa_links', 'user_email VARCHAR(255)')
            _add_col_if_missing('wa_links', 'wa_number VARCHAR(40)')
            _add_col_if_missing('wa_links', 'user_id INTEGER')
            _add_col_if_missing('wa_links', 'created_at TIMESTAMP')

    except Exception as e:
        print('DB create_all failed:', repr(e))


with app.app_context():
    _create_tables_if_needed()


# -------------------------
# Helpers
# -------------------------
def _hash_password(pw: str) -> str:
    return hashlib.sha256((pw or '').encode('utf-8')).hexdigest()


def _get_logged_email() -> str | None:
    return session.get('user_email')


def _require_login() -> str | None:
    return _get_logged_email()


def _parse_brl_value(text_value: str) -> Decimal:
    if text_value is None:
        raise ValueError('valor vazio')

    s = str(text_value).strip()
    if not s:
        raise ValueError('valor vazio')

    s = re.sub(r'[^0-9,\.-]', '', s)

    if '.' in s and ',' in s:
        s = s.replace('.', '').replace(',', '.')
    else:
        if ',' in s and '.' not in s:
            s = s.replace(',', '.')

    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError('valor inválido')


def _parse_date_any(d: str | None) -> date:
    if not d:
        return datetime.utcnow().date()
    s = str(d).strip()
    try:
        if re.match(r'^\d{4}-\d{2}-\d{2}$', s):
            return datetime.strptime(s, '%Y-%m-%d').date()
        if re.match(r'^\d{2}/\d{2}/\d{4}$', s):
            return datetime.strptime(s, '%d/%m/%Y').date()
    except Exception:
        pass
    return datetime.utcnow().date()


def _get_or_create_user(email: str, password: str | None = None, extras: dict | None = None) -> User:
    user = User.query.filter_by(email=email).first()
    if user:
        # atualiza extras se vierem
        if extras:
            for k, v in extras.items():
                if hasattr(user, k) and v:
                    setattr(user, k, v)
            db.session.commit()
        return user

    if password is None:
        pw_hash = _hash_password(os.urandom(16).hex())
        user = User(email=email, password_hash=pw_hash, password_set=False)
    else:
        pw_hash = _hash_password(password)
        user = User(email=email, password_hash=pw_hash, password_set=True)

    if extras:
        for k, v in extras.items():
            if hasattr(user, k) and v:
                setattr(user, k, v)

    db.session.add(user)
    db.session.commit()
    return user


def _status_payload():
    return {
        'ok': True,
        'db_enabled': DB_ENABLED,
        'db_uri_set': bool(_raw_db_url),
    }


def _get_password_from_payload(data: dict) -> str:
    """
    ✅ Compatível com seu FRONT:
      - senha
      - password
    """
    return (data.get('senha') or data.get('password') or '').strip()


def _get_new_password_from_payload(data: dict) -> str:
    """
    ✅ Compatível com seu FRONT:
      - nova_senha
      - newPassword
      - password
    """
    return (data.get('nova_senha') or data.get('newPassword') or data.get('password') or '').strip()


# -------------------------
# Static / Frontend
# -------------------------
@app.get('/')
def home():
    return render_template('index.html')


@app.get('/offline.html')
def offline_page():
    return render_template('offline.html')


@app.get('/manifest.json')
def manifest():
    return send_from_directory(app.static_folder, 'manifest.json')


@app.get('/sw.js')
def service_worker():
    resp = send_from_directory(app.static_folder, 'sw.js')
    resp.headers['Content-Type'] = 'application/javascript; charset=utf-8'
    return resp


@app.get('/robots.txt')
def robots():
    return send_from_directory(app.static_folder, 'robots.txt')


@app.get('/health')
def health():
    return jsonify(_status_payload())


# -------------------------
# Auth API
# -------------------------
@app.post('/api/register')
def api_register():
    data = request.get_json(silent=True) or {}

    email = (data.get('email') or '').strip().lower()
    password = _get_password_from_payload(data)

    if not email or '@' not in email:
        return jsonify({'error': 'Email inválido'}), 400

    # ✅ seu front manda senha forte; aqui só valida mínimo (evita falso "curta")
    if len(password) < 4:
        return jsonify({'error': 'Senha muito curta'}), 400

    existing = User.query.filter_by(email=email).first()
    if existing:
        # Se foi criado automático (WhatsApp), permitir “reivindicar” definindo senha
        if getattr(existing, 'password_set', False) is False:
            existing.password_hash = _hash_password(password)
            existing.password_set = True
            # salva extras se vierem
            for k in ('nome_apelido', 'nome_completo', 'telefone'):
                if k in data and getattr(existing, k, None) is not None:
                    setattr(existing, k, (data.get(k) or '').strip() or getattr(existing, k))
            db.session.commit()
            session['user_email'] = email
            return jsonify({'ok': True, 'email': email, 'claimed': True})
        return jsonify({'error': 'Email já cadastrado'}), 409

    extras = {
        'nome_apelido': (data.get('nome_apelido') or '').strip(),
        'nome_completo': (data.get('nome_completo') or '').strip(),
        'telefone': (data.get('telefone') or '').strip(),
    }
    user = _get_or_create_user(email, password=password, extras=extras)
    session['user_email'] = email
    return jsonify({'ok': True, 'email': email})


@app.post('/api/reset_password')
def api_reset_password():
    data = request.get_json(silent=True) or {}

    email = (data.get('email') or '').strip().lower()
    new_password = _get_new_password_from_payload(data)

    if not email or '@' not in email:
        return jsonify({'error': 'Email inválido'}), 400
    if len(new_password) < 4:
        return jsonify({'error': 'Senha muito curta'}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({'error': 'Usuário não encontrado'}), 404

    user.password_hash = _hash_password(new_password)
    user.password_set = True
    db.session.commit()
    return jsonify({'ok': True})


@app.post('/api/login')
def api_login():
    data = request.get_json(silent=True) or {}

    email = (data.get('email') or '').strip().lower()
    password = _get_password_from_payload(data)

    user = User.query.filter_by(email=email).first()
    if not user or user.password_hash != _hash_password(password):
        return jsonify({'error': 'Credenciais inválidas'}), 401

    session['user_email'] = email
    return jsonify({'ok': True, 'email': email})


@app.post('/api/logout')
def api_logout():
    session.pop('user_email', None)
    return jsonify({'ok': True})


@app.get('/api/me')
def api_me():
    email = _get_logged_email()
    return jsonify({'email': email})


# -------------------------
# Transactions API
# -------------------------
@app.get('/api/lancamentos')
def api_list_lancamentos():
    email = _require_login()
    if not email:
        return jsonify({'error': 'Você precisa estar logado.'}), 401

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({'items': []})

    limit = int(request.args.get('limit', 30))
    limit = max(1, min(limit, 200))

    rows = (
        Transaction.query
        .filter_by(user_id=user.id)
        .order_by(Transaction.data.desc(), Transaction.id.desc())
        .limit(limit)
        .all()
    )

    items = []
    for t in rows:
        items.append({
            'id': t.id,
            'tipo': t.tipo,
            'data': t.data.isoformat(),
            'categoria': t.categoria,
            'descricao': t.descricao or '',
            'valor': float(t.valor),
            'origem': t.origem,
            'created_at': t.created_at.isoformat() if t.created_at else None,
        })

    return jsonify({'items': items})


@app.post('/api/lancamentos')
def api_create_lancamento():
    email = _require_login()
    if not email:
        return jsonify({'error': 'Você precisa estar logado.'}), 401

    data = request.get_json(silent=True) or {}

    tipo = (data.get('tipo') or '').strip().upper()
    if tipo not in ('RECEITA', 'GASTO'):
        return jsonify({'error': 'Tipo inválido'}), 400

    categoria = (data.get('categoria') or '').strip() or 'Outros'
    descricao = (data.get('descricao') or '').strip() or None

    # ✅ aceita "data" do front
    d = _parse_date_any(data.get('data') or data.get('date'))

    try:
        valor = _parse_brl_value(data.get('valor'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    user = _get_or_create_user(email)

    t = Transaction(
        user_id=user.id,
        tipo=tipo,
        data=d,          # ✅ coluna correta
        categoria=categoria,
        descricao=descricao,
        valor=valor,
        origem='APP',
    )
    db.session.add(t)
    db.session.commit()

    return jsonify({'ok': True, 'id': t.id})


@app.delete('/api/lancamentos/<int:tx_id>')
def api_delete_lancamento(tx_id: int):
    email = _require_login()
    if not email:
        return jsonify({'error': 'Você precisa estar logado.'}), 401

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({'error': 'Usuário não encontrado'}), 404

    t = Transaction.query.filter_by(id=tx_id, user_id=user.id).first()
    if not t:
        return jsonify({'error': 'Lançamento não encontrado'}), 404

    db.session.delete(t)
    db.session.commit()
    return jsonify({'ok': True})


@app.get('/api/dashboard')
def api_dashboard():
    email = _require_login()
    if not email:
        return jsonify({'error': 'Você precisa estar logado.'}), 401

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({'receitas': 0.0, 'gastos': 0.0, 'saldo': 0.0})

    try:
        ano = int(request.args.get('ano', datetime.utcnow().year))
        mes = int(request.args.get('mes', datetime.utcnow().month))
    except Exception:
        ano = datetime.utcnow().year
        mes = datetime.utcnow().month

    start = date(ano, mes, 1)
    end = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)

    q = (
        Transaction.query
        .filter(Transaction.user_id == user.id)
        .filter(Transaction.data >= start)   # ✅ coluna correta
        .filter(Transaction.data < end)
    )

    receitas = Decimal('0')
    gastos = Decimal('0')
    for t in q.all():
        if t.tipo == 'RECEITA':
            receitas += Decimal(t.valor)
        else:
            gastos += Decimal(t.valor)

    saldo = receitas - gastos

    return jsonify({
        'receitas': float(receitas),
        'gastos': float(gastos),
        'saldo': float(saldo),
        'mes': mes,
        'ano': ano,
    })


# -------------------------
# WhatsApp Cloud API Webhook (mantido, com compat DB)
# -------------------------
WA_VERIFY_TOKEN = os.getenv('WA_VERIFY_TOKEN', '').strip()


def _parse_wa_text_to_transaction(msg_text: str):
    text_msg = (msg_text or '').strip()
    if not text_msg:
        return None

    lower = text_msg.lower()
    parts = re.split(r'\s+', lower)
    if not parts:
        return None

    if len(parts) == 1 and '@' in parts[0] and '.' in parts[0]:
        return {'cmd': 'LINK_EMAIL', 'email': parts[0]}

    tipo = None
    if parts[0] in ('gasto', 'despesa', 'saida', 'saída'):
        tipo = 'GASTO'
        parts = parts[1:]
    elif parts[0] in ('receita', 'entrada'):
        tipo = 'RECEITA'
        parts = parts[1:]

    if tipo is None and parts and parts[0] in ('salario', 'salário'):
        tipo = 'RECEITA'
    if tipo is None:
        tipo = 'GASTO'

    value_token = None
    rest = []
    for p in parts:
        if value_token is None and re.search(r'\d', p):
            value_token = p
        else:
            rest.append(p)

    if not value_token:
        return None

    try:
        valor = _parse_brl_value(value_token)
    except Exception:
        return None

    categoria = (rest[0] if rest else 'Outros').capitalize()
    descricao = ' '.join(rest[1:]).strip() if len(rest) > 1 else None

    return {
        'cmd': 'TX',
        'tipo': tipo,
        'valor': valor,
        'categoria': categoria,
        'descricao': descricao,
        'data': datetime.utcnow().date(),
    }


@app.get('/webhooks/whatsapp')
def wa_verify():
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')

    if mode == 'subscribe' and token and token == WA_VERIFY_TOKEN:
        return challenge or '', 200
    return 'Forbidden', 403


@app.post('/webhooks/whatsapp')
def wa_webhook():
    payload = request.get_json(silent=True) or {}

    try:
        entry = (payload.get('entry') or [])[0]
        changes = (entry.get('changes') or [])[0]
        value = changes.get('value') or {}
        messages = value.get('messages') or []
        if not messages:
            return jsonify({'ok': True})

        msg = messages[0]
        msg_id = msg.get('id')
        wa_from = msg.get('from')
        msg_text = ((msg.get('text') or {}).get('body') or '').strip()

        if msg_id and ProcessedMessage.query.filter_by(msg_id=msg_id).first():
            return jsonify({'ok': True})

        if msg_id:
            db.session.add(ProcessedMessage(msg_id=msg_id, wa_from=wa_from))
            db.session.commit()

        parsed = _parse_wa_text_to_transaction(msg_text)
        if not parsed:
            return jsonify({'ok': True})

        if parsed.get('cmd') == 'LINK_EMAIL':
            email = parsed.get('email')
            if email and wa_from:
                link = WaLink.query.filter_by(wa_from=wa_from).first()
                if link:
                    link.user_email = email
                else:
                    link = WaLink(wa_from=wa_from, user_email=email, wa_number=wa_from)
                    db.session.add(link)
                db.session.commit()
            return jsonify({'ok': True})

        link = WaLink.query.filter_by(wa_from=wa_from).first()
        if not link or not link.user_email:
            return jsonify({'ok': True})

        user = _get_or_create_user(link.user_email)

        t = Transaction(
            user_id=user.id,
            tipo=parsed['tipo'],
            data=parsed['data'],      # ✅ coluna correta
            categoria=parsed['categoria'],
            descricao=parsed.get('descricao'),
            valor=parsed['valor'],
            origem='WA',
        )
        db.session.add(t)
        db.session.commit()

        return jsonify({'ok': True})

    except Exception as e:
        print('WA webhook error:', repr(e))
        return jsonify({'ok': True})


# -------------------------
# Entry
# -------------------------
if __name__ == '__main__':
    port = int(os.getenv('PORT', '8080'))
    app.run(host='0.0.0.0', port=port)

