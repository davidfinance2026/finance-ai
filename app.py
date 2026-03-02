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

_raw_db_url = os.getenv('DATABASE_URL', '').strip()
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

# Cookies de sessão em HTTPS (Railway)
app.config['SESSION_COOKIE_SECURE'] = True
# Em geral, Lax funciona bem para app no mesmo domínio.
# Se você estiver usando iframe / cross-site, troque para 'None' (e mantenha Secure=True).
app.config['SESSION_COOKIE_SAMESITE'] = os.getenv('SESSION_SAMESITE', 'Lax')

# Segurança e UX
MIN_PASSWORD_LEN = int(os.getenv('MIN_PASSWORD_LEN', '6'))
PANIC_TOKEN = (os.getenv('PANIC_TOKEN') or '').strip()

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

    # coluna real no Postgres é "data"
    date = db.Column('data', db.Date, nullable=False, index=True)

    categoria = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.Text, nullable=True)

    # coluna real no Postgres é "valor"
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

        # migrações leves
        if 'users' in table_names:
            _run("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_set BOOLEAN NOT NULL DEFAULT false")

        if 'wa_links' in table_names:
            _run("ALTER TABLE wa_links ADD COLUMN IF NOT EXISTS wa_from VARCHAR(40)")
            _run("ALTER TABLE wa_links ADD COLUMN IF NOT EXISTS user_email VARCHAR(255)")
            _run("ALTER TABLE wa_links ADD COLUMN IF NOT EXISTS created_at TIMESTAMP")

        if 'processed_messages' in table_names:
            _run("ALTER TABLE processed_messages ADD COLUMN IF NOT EXISTS wa_from VARCHAR(40)")

        # garantir que transactions tem data/valor
        if 'transactions' in table_names:
            _run("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS data DATE")
            _run("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS valor NUMERIC(12,2)")

            # tenta converter caso estejam como texto
            _run("""
            DO $$
            BEGIN
              BEGIN
                ALTER TABLE transactions ALTER COLUMN data TYPE DATE USING data::date;
              EXCEPTION WHEN others THEN
              END;

              BEGIN
                ALTER TABLE transactions ALTER COLUMN valor TYPE NUMERIC(12,2) USING valor::numeric;
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

    s = re.sub(r'[^0-9,\.-]', '', s)

    # 1.234,56 -> 1234.56
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


def _status_payload():
    return {'ok': True, 'db_enabled': DB_ENABLED, 'db_uri_set': bool(_raw_db_url)}


def _get_password_from_payload(data: dict) -> str:
    # Aceita várias chaves (pra bater com seu frontend)
    return (data.get('password') or data.get('senha') or data.get('newPassword') or '').strip()


def _validate_password(pw: str) -> tuple[bool, str]:
    if len(pw) < MIN_PASSWORD_LEN:
        return False, f'Senha muito curta (mínimo {MIN_PASSWORD_LEN} caracteres)'
    return True, ''


def _get_user_by_email(email: str) -> User | None:
    return User.query.filter_by(email=email).first()


def _create_user(email: str, password: str) -> User:
    user = User(email=email, password_hash=_hash_password(password), password_set=True)
    db.session.add(user)
    db.session.commit()
    return user


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

    ok, msg = _validate_password(password)
    if not ok:
        return jsonify({'error': msg}), 400

    existing = _get_user_by_email(email)
    if existing:
        # se existia “sem senha” (legado), permite “reivindicar”
        if getattr(existing, 'password_set', False) is False:
            existing.password_hash = _hash_password(password)
            existing.password_set = True
            db.session.commit()
            session['user_email'] = email
            return jsonify({'ok': True, 'email': email, 'claimed': True})
        return jsonify({'error': 'Email já cadastrado'}), 409

    _create_user(email, password)
    session['user_email'] = email
    return jsonify({'ok': True, 'email': email})


@app.post('/api/reset_password')
def api_reset_password():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    new_password = _get_password_from_payload(data)

    if not email or '@' not in email:
        return jsonify({'error': 'Email inválido'}), 400

    ok, msg = _validate_password(new_password)
    if not ok:
        return jsonify({'error': msg}), 400

    user = _get_user_by_email(email)
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

    user = _get_user_by_email(email)
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
# PANIC BUTTON (zera tudo)
# -------------------------
def _panic_token_ok(req) -> bool:
    token = (req.headers.get('X-Panic-Token') or req.args.get('token') or '').strip()
    return bool(PANIC_TOKEN) and token == PANIC_TOKEN


@app.post('/api/panic_wipe')
def api_panic_wipe():
    """
    Zera as tabelas principais.
    Segurança: exige PANIC_TOKEN (env) e envio via header X-Panic-Token ou ?token=
    """
    if not _panic_token_ok(request):
        return jsonify({'error': 'Não autorizado'}), 401

    try:
        # ordem: dependências primeiro
        deleted = {}

        deleted['processed_messages'] = db.session.query(ProcessedMessage).delete()
        deleted['wa_links'] = db.session.query(WaLink).delete()
        deleted['transactions'] = db.session.query(Transaction).delete()
        deleted['users'] = db.session.query(User).delete()

        db.session.commit()
        session.pop('user_email', None)

        return jsonify({'ok': True, 'deleted': deleted})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Falha ao limpar', 'detail': repr(e)}), 500


# -------------------------
# Transactions API
# -------------------------
@app.get('/api/lancamentos')
def api_list_lancamentos():
    email = _require_login()
    if not email:
        return jsonify({'error': 'Você precisa estar logado.'}), 401

    user = _get_user_by_email(email)
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

    user = _get_user_by_email(email)
    if not user:
        # Não cria usuário “fantasma”
        return jsonify({'error': 'Sessão inválida. Faça login novamente.'}), 401

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

    user = _get_user_by_email(email)
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

    user = _get_user_by_email(email)
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
WA_VERIFY_TOKEN = os.getenv('WA_VERIFY_TOKEN', '').strip()


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

        user = _get_user_by_email(link.user_email)
        if not user:
            # se não existir user, não cria automaticamente aqui
            return jsonify({'ok': True})

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
