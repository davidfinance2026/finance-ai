import os
import re
import json
import hashlib
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

from flask import Flask, request, jsonify, send_from_directory, session
from flask_sqlalchemy import SQLAlchemy

# -------------------------
# App / Config
# -------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder='.', static_url_path='')
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key-change-me')

# Railway often provides DATABASE_URL or you create your own.
_raw_db_url = os.getenv('DATABASE_URL', '').strip()
if _raw_db_url.startswith('postgres://'):
    _raw_db_url = _raw_db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = _raw_db_url or 'sqlite:///' + os.path.join(BASE_DIR, 'local.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

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
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Transaction(db.Model):
    __tablename__ = 'transactions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    # 'RECEITA' | 'GASTO'
    tipo = db.Column(db.String(16), nullable=False)
    # We use date (not "data") to avoid confusion.
    date = db.Column(db.Date, nullable=False, index=True)

    categoria = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    valor = db.Column(db.Numeric(12, 2), nullable=False)

    origem = db.Column(db.String(16), nullable=False, default='APP')  # APP | WA
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
    """Create tables (simple approach: no migrations)."""
    try:
        db.create_all()
    except Exception as e:
        # If DB isn't reachable, app should still boot and show a helpful status.
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
    email = _get_logged_email()
    return email


def _parse_brl_value(text: str) -> Decimal:
    """Accepts: '45', '45,90', '45.90', '1.234,56', '1234.56'. Returns Decimal with dot as separator."""
    if text is None:
        raise ValueError('valor vazio')

    s = str(text).strip()
    if not s:
        raise ValueError('valor vazio')

    # keep digits, ',' '.' and '-'
    s = re.sub(r'[^0-9,\.-]', '', s)

    # If has both '.' and ',', assume '.' is thousands and ',' decimal -> remove '.'
    if '.' in s and ',' in s:
        s = s.replace('.', '').replace(',', '.')
    else:
        # If only comma -> decimal comma
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
    # Accept YYYY-MM-DD
    try:
        if re.match(r'^\d{4}-\d{2}-\d{2}$', s):
            return datetime.strptime(s, '%Y-%m-%d').date()
        # Accept DD/MM/YYYY
        if re.match(r'^\d{2}/\d{2}/\d{4}$', s):
            return datetime.strptime(s, '%d/%m/%Y').date()
    except Exception:
        pass
    return datetime.utcnow().date()


def _get_or_create_user(email: str, password: str | None = None) -> User:
    user = User.query.filter_by(email=email).first()
    if user:
        return user

    # If user doesn't exist and no password provided, create a placeholder (used only for WA link edge cases)
    pw_hash = _hash_password(password or os.urandom(16).hex())
    user = User(email=email, password_hash=pw_hash)
    db.session.add(user)
    db.session.commit()
    return user


def _status_payload():
    return {
        'ok': True,
        'db_enabled': DB_ENABLED,
        'db_uri_set': bool(_raw_db_url),
    }


# -------------------------
# Static / Frontend
# -------------------------

@app.get('/')
def home():
    # If you use a SPA, change to send your index.html.
    # Current project also works as API-only; front-end can be hosted elsewhere.
    index_path = os.path.join(BASE_DIR, 'index.html')
    if os.path.exists(index_path):
        return send_from_directory(BASE_DIR, 'index.html')
    return 'Finance AI 🚀 Backend funcionando corretamente.', 200


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
    password = (data.get('password') or '').strip()

    if not email or '@' not in email:
        return jsonify({'error': 'Email inválido'}), 400
    if len(password) < 4:
        return jsonify({'error': 'Senha muito curta'}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email já cadastrado'}), 409

    user = User(email=email, password_hash=_hash_password(password))
    db.session.add(user)
    db.session.commit()

    session['user_email'] = email
    return jsonify({'ok': True, 'email': email})


@app.post('/api/login')
def api_login():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = (data.get('password') or '').strip()

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
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(limit)
        .all()
    )

    items = []
    for t in rows:
        items.append({
            'id': t.id,
            'tipo': t.tipo,
            'data': t.date.isoformat(),
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
    if mes == 12:
        end = date(ano + 1, 1, 1)
    else:
        end = date(ano, mes + 1, 1)

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
# WhatsApp Cloud API Webhook
# -------------------------
WA_VERIFY_TOKEN = os.getenv('WA_VERIFY_TOKEN', '').strip()


def _parse_wa_text_to_transaction(msg_text: str):
    """Parse patterns like:
    - 'gasto 32,90 mercado'
    - 'salário 100'
    - 'receita 1000 salario'

    Returns dict or None.
    """
    text = (msg_text or '').strip()
    if not text:
        return None

    lower = text.lower()

    # Normalize: split by spaces
    parts = re.split(r'\s+', lower)
    if not parts:
        return None

    # If user sends only "email@..." we treat as link command
    if len(parts) == 1 and '@' in parts[0] and '.' in parts[0]:
        return {'cmd': 'LINK_EMAIL', 'email': parts[0]}

    tipo = None
    if parts[0] in ('gasto', 'despesa', 'saida', 'saída'):
        tipo = 'GASTO'
        parts = parts[1:]
    elif parts[0] in ('receita', 'entrada'):
        tipo = 'RECEITA'
        parts = parts[1:]

    # Allow "salário 100" meaning receita
    if tipo is None and parts and parts[0] in ('salario', 'salário'):
        tipo = 'RECEITA'

    if tipo is None:
        # Default: gasto
        tipo = 'GASTO'

    # Find first number token as value
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
    # Meta verify
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

        # de-dup
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
                # upsert link
                link = WaLink.query.filter_by(wa_from=wa_from).first()
                if link:
                    link.user_email = email
                else:
                    link = WaLink(wa_from=wa_from, user_email=email)
                    db.session.add(link)
                db.session.commit()
            return jsonify({'ok': True})

        # Normal transaction
        link = WaLink.query.filter_by(wa_from=wa_from).first()
        if not link:
            # Not linked yet
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
