import os
import re
import hashlib
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

from flask import Flask, request, jsonify, send_from_directory, session, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

# -------------------------
# App / Config
# -------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key-change-me')

_raw_db_url = (os.getenv('DATABASE_URL') or '').strip()
if _raw_db_url.startswith('postgres://'):
    _raw_db_url = _raw_db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = _raw_db_url or 'sqlite:///' + os.path.join(BASE_DIR, 'local.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 280,
    'pool_size': int(os.getenv('DB_POOL_SIZE', '3')),
    'max_overflow': int(os.getenv('DB_MAX_OVERFLOW', '2')),
}

DB_ENABLED = bool(_raw_db_url)

# Cookies de sessão (Railway usa HTTPS). Para PWA/mobile normalmente Lax funciona.
# Se algum navegador bloquear sessão, você pode setar SESSION_COOKIE_SAMESITE=None via env.
_cookie_samesite = (os.getenv('SESSION_COOKIE_SAMESITE') or 'Lax').strip()
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_SAMESITE'] = _cookie_samesite

# Ajuste de tamanho mínimo de senha (pra não ficar “curta”).
# IMPORTANTE: o problema que você viu geralmente é o front mandando chave errada (senha vs password),
# então aqui aceitamos várias chaves.
MIN_PASSWORD_LEN = int(os.getenv('MIN_PASSWORD_LEN', '4'))  # pode subir pra 6/8 depois se quiser

# -------------------------
# DB
# -------------------------
db = SQLAlchemy(app)


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)

    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(64), nullable=False)
    password_set = db.Column(db.Boolean, nullable=False, server_default=text('false'))

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Transaction(db.Model):
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    tipo = db.Column(db.String(16), nullable=False)

    # No Postgres a coluna real é "data" (pelo seu print)
    date = db.Column('data', db.Date, nullable=False, index=True)

    categoria = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.Text, nullable=True)

    # No Postgres a coluna real é "valor"
    valor = db.Column('valor', db.Numeric(12, 2), nullable=False)

    origem = db.Column(db.String(16), nullable=False, default='APP')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class WaLink(db.Model):
    __tablename__ = 'wa_links'
    id = db.Column(db.Integer, primary_key=True)

    wa_from = db.Column(db.String(40), unique=True, nullable=False, index=True)
    user_email = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ProcessedMessage(db.Model):
    __tablename__ = 'processed_messages'
    id = db.Column(db.Integer, primary_key=True)

    msg_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    wa_from = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


def _create_tables_if_needed() -> None:
    try:
        db.create_all()

        insp = inspect(db.engine)
        table_names = set(insp.get_table_names())

        def _run(sql: str):
            try:
                db.session.execute(text(sql))
                db.session.commit()
            except Exception:
                db.session.rollback()

        # migrações leves / garantias
        if 'users' in table_names:
            _run("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_set BOOLEAN NOT NULL DEFAULT false")

        if 'wa_links' in table_names:
            _run("ALTER TABLE wa_links ADD COLUMN IF NOT EXISTS wa_from VARCHAR(40)")
            _run("ALTER TABLE wa_links ADD COLUMN IF NOT EXISTS user_email VARCHAR(255)")
            _run("ALTER TABLE wa_links ADD COLUMN IF NOT EXISTS created_at TIMESTAMP")

        if 'processed_messages' in table_names:
            _run("ALTER TABLE processed_messages ADD COLUMN IF NOT EXISTS wa_from VARCHAR(40)")

        if 'transactions' in table_names:
            # garante as colunas esperadas no Postgres
            _run("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS data DATE")
            _run("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS valor NUMERIC(12,2)")
            _run("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS tipo VARCHAR(16)")
            _run("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS categoria VARCHAR(80)")
            _run("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS descricao TEXT")
            _run("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS origem VARCHAR(16)")
            _run("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP")

            # tenta converter tipos se tiver DB antigo com texto
            _run("""
            DO $$
            BEGIN
              BEGIN
                ALTER TABLE transactions ALTER COLUMN data TYPE DATE USING NULLIF(data,'')::date;
              EXCEPTION WHEN others THEN
              END;

              BEGIN
                ALTER TABLE transactions ALTER COLUMN valor TYPE NUMERIC(12,2) USING REPLACE(NULLIF(valor::text,''), ',', '.')::numeric;
              EXCEPTION WHEN others THEN
              END;
            END$$;
            """)

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


def _parse_brl_value(textv: str) -> Decimal:
    if textv is None:
        raise ValueError('valor vazio')

    s = str(textv).strip()
    if not s:
        raise ValueError('valor vazio')

    # mantém números e separadores
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


def _safe_date_iso(v) -> str:
    # evita crash "str has no attribute isoformat"
    if v is None:
        return datetime.utcnow().date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    try:
        return _parse_date_any(str(v)).isoformat()
    except Exception:
        return datetime.utcnow().date().isoformat()


def _get_password_from_payload(data: dict) -> str:
    # aceita várias chaves (corrige front mandando "senha" ao invés de "password")
    candidates = [
        data.get('password'),
        data.get('senha'),
        data.get('newPassword'),
        data.get('new_password'),
        data.get('pass'),
    ]
    for c in candidates:
        if c is not None:
            return str(c).strip()
    return ''


def _get_or_create_user(email: str, password: str | None = None) -> User:
    user = User.query.filter_by(email=email).first()
    if user:
        return user

    if password is None:
        pw_hash = _hash_password(os.urandom(16).hex())
        user = User(email=email, password_hash=pw_hash, password_set=False)
    else:
        pw_hash = _hash_password(password)
        user = User(email=email, password_hash=pw_hash, password_set=True)

    db.session.add(user)
    db.session.commit()
    return user


def _status_payload():
    return {
        'ok': True,
        'db_enabled': DB_ENABLED,
        'db_uri_set': bool(_raw_db_url),
        'cookie_samesite': app.config.get('SESSION_COOKIE_SAMESITE'),
        'min_password_len': MIN_PASSWORD_LEN,
        'panic_enabled': bool(os.getenv('PANIC_TOKEN')),
    }

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
# Panic reset (Botão de Pânico)
# -------------------------
def _panic_allowed() -> bool:
    token = (os.getenv('PANIC_TOKEN') or '').strip()
    if not token:
        return False
    got = (request.headers.get('X-Panic-Token') or '').strip()
    return got and got == token


@app.post('/api/panic_reset')
def api_panic_reset():
    """
    APAGA TUDO (users, transactions, wa_links, processed_messages).
    Protegido por X-Panic-Token e exige {"confirm": true}.
    """
    if not _panic_allowed():
        return jsonify({'error': 'Não autorizado'}), 401

    data = request.get_json(silent=True) or {}
    if data.get('confirm') is not True:
        return jsonify({'error': 'Confirmação necessária: envie {"confirm": true}'}), 400

    try:
        # TRUNCATE com CASCADE para limpar e resetar IDs
        db.session.execute(text("TRUNCATE TABLE transactions RESTART IDENTITY CASCADE"))
        db.session.execute(text("TRUNCATE TABLE wa_links RESTART IDENTITY CASCADE"))
        db.session.execute(text("TRUNCATE TABLE processed_messages RESTART IDENTITY CASCADE"))
        db.session.execute(text("TRUNCATE TABLE users RESTART IDENTITY CASCADE"))
        db.session.commit()

        # também limpa sessão do usuário atual
        session.pop('user_email', None)

        return jsonify({'ok': True, 'message': 'Reset completo executado. Todas as tabelas foram limpas.'})
    except Exception as e:
        db.session.rollback()
        print('panic_reset error:', repr(e))
        return jsonify({'error': 'Falha ao executar reset', 'detail': str(e)}), 500


@app.get('/api/panic_status')
def api_panic_status():
    return jsonify({'panic_enabled': bool(os.getenv('PANIC_TOKEN'))})

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

    if len(password) < MIN_PASSWORD_LEN:
        return jsonify({'error': f'Senha muito curta (mín {MIN_PASSWORD_LEN})'}), 400

    existing = User.query.filter_by(email=email).first()
    if existing:
        # Se existir e password_set for False, “reivindica” a conta
        if getattr(existing, 'password_set', False) is False:
            existing.password_hash = _hash_password(password)
            existing.password_set = True
            db.session.commit()
            session['user_email'] = email
            return jsonify({'ok': True, 'email': email, 'claimed': True})
        return jsonify({'error': 'Email já cadastrado'}), 409

    user = _get_or_create_user(email, password=password)
    session['user_email'] = email
    return jsonify({'ok': True, 'email': user.email})


@app.post('/api/reset_password')
def api_reset_password():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    new_password = _get_password_from_payload(data)

    if not email or '@' not in email:
        return jsonify({'error': 'Email inválido'}), 400
    if len(new_password) < MIN_PASSWORD_LEN:
        return jsonify({'error': f'Senha muito curta (mín {MIN_PASSWORD_LEN})'}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({'error': 'Usuário não encontrado'}), 404

    user.password_hash = _hash_password(new_password)
    user.password_set = True
    db.session.commit()

    # opcional: loga direto após reset
    session['user_email'] = email

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
    return jsonify({'email': _get_logged_email()})

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
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(limit)
        .all()
    )

    items = []
    for t in rows:
        items.append({
            'id': t.id,
            'tipo': t.tipo,
            'data': _safe_date_iso(t.date),
            'categoria': t.categoria,
            'descricao': t.descricao or '',
            'valor': float(t.valor) if t.valor is not None else 0.0,
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
    d = _parse_date_any(data.get('data'))

    try:
        valor = _parse_brl_value(data.get('valor'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    user = _get_or_create_user(email)

    t = Transaction(
        user_id=user.id,
        tipo=tipo,
        date=d,
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
        .filter(Transaction.date >= start)
        .filter(Transaction.date < end)
    )

    receitas = Decimal('0')
    gastos = Decimal('0')
    for t in q.all():
        if t.tipo == 'RECEITA':
            receitas += Decimal(t.valor or 0)
        else:
            gastos += Decimal(t.valor or 0)

    saldo = receitas - gastos

    return jsonify({
        'receitas': float(receitas),
        'gastos': float(gastos),
        'saldo': float(saldo),
        'mes': mes,
        'ano': ano,
    })

# -------------------------
# WhatsApp Cloud API Webhook
# -------------------------
WA_VERIFY_TOKEN = (os.getenv('WA_VERIFY_TOKEN') or '').strip()


def _parse_wa_text_to_transaction(msg_text: str):
    textv = (msg_text or '').strip()
    if not textv:
        return None

    lower = textv.lower()
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
            if email:
                link = WaLink.query.filter_by(wa_from=wa_from).first()
                if link:
                    link.user_email = email
                else:
                    link = WaLink(wa_from=wa_from, user_email=email)
                    db.session.add(link)
                db.session.commit()
            return jsonify({'ok': True})

        link = WaLink.query.filter_by(wa_from=wa_from).first()
        if not link:
            return jsonify({'ok': True})

        user = _get_or_create_user(link.user_email)

        t = Transaction(
            user_id=user.id,
            tipo=parsed['tipo'],
            date=parsed['data'],
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
