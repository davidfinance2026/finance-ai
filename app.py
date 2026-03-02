import os
import re
from datetime import datetime, date
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from werkzeug.security import generate_password_hash, check_password_hash

# ---------------------------
# Config
# ---------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
# Railway às vezes entrega postgres:// e o SQLAlchemy quer postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret").strip()
PANIC_TOKEN = os.getenv("PANIC_TOKEN", "").strip()  # obrigatório para usar /api/panic/wipe
PASSWORD_MIN_LEN = int(os.getenv("PASSWORD_MIN_LEN", "8"))

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = SECRET_KEY

db = SQLAlchemy(app)

# ---------------------------
# Models
# ---------------------------
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    nickname = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(40), nullable=True)
    full_name = db.Column(db.String(200), nullable=True)
    email = db.Column(db.String(200), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Transaction(db.Model):
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    data = db.Column(db.Date, nullable=False, index=True)  # IMPORTANTE: tipo Date no banco
    tipo = db.Column(db.String(20), nullable=False)  # RECEITA ou GASTO
    categoria = db.Column(db.String(120), nullable=False)
    descricao = db.Column(db.String(300), nullable=True)
    valor = db.Column(db.Numeric(14, 2), nullable=False)
    origem = db.Column(db.String(20), nullable=True)  # APP/WA
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


# Se você tem essas tabelas no Postgres (pelo print), deixo modelos simples só p/ TRUNCATE funcionar
class WALink(db.Model):
    __tablename__ = "wa_links"
    id = db.Column(db.Integer, primary_key=True)


class ProcessedMessage(db.Model):
    __tablename__ = "processed_messages"
    id = db.Column(db.Integer, primary_key=True)


# ---------------------------
# Helpers
# ---------------------------
def json_error(message, status=400, **extra):
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return jsonify(payload), status


def parse_date_yyyy_mm_dd(s: str) -> date:
    # aceita "2026-03-01"
    return datetime.strptime(s, "%Y-%m-%d").date()


def validate_password(pw: str):
    """
    Política:
    - mínimo PASSWORD_MIN_LEN (padrão 8)
    - 1 minúscula, 1 maiúscula, 1 número, 1 especial
    """
    if pw is None:
        return False, "Senha ausente."
    # NÃO use strip() aqui, porque tiraria espaços e mudaria o tamanho percebido
    if len(pw) < PASSWORD_MIN_LEN:
        return False, f"Senha muito curta. Mínimo {PASSWORD_MIN_LEN} caracteres."

    if not re.search(r"[a-z]", pw):
        return False, "A senha precisa ter pelo menos 1 letra minúscula."
    if not re.search(r"[A-Z]", pw):
        return False, "A senha precisa ter pelo menos 1 letra maiúscula."
    if not re.search(r"[0-9]", pw):
        return False, "A senha precisa ter pelo menos 1 número."
    if not re.search(r"[^A-Za-z0-9]", pw):
        return False, "A senha precisa ter pelo menos 1 caractere especial."

    return True, None


def require_panic_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not PANIC_TOKEN:
            return json_error("PANIC_TOKEN não configurado no servidor.", 403)

        token = request.headers.get("X-Panic-Token", "")
        if token != PANIC_TOKEN:
            return json_error("Token inválido.", 403)
        return fn(*args, **kwargs)
    return wrapper


# Sessão simples (cookie) — se você já usa outro método, dá pra adaptar,
# mas isso aqui resolve o básico sem complicar.
from flask import session

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return db.session.get(User, uid)


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            return json_error("Não autenticado.", 401)
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------
# Routes - Front
# ---------------------------
@app.get("/")
def index():
    # se seu front é SPA simples
    return send_from_directory(BASE_DIR, "index.html")


# ---------------------------
# Routes - Auth
# ---------------------------
@app.post("/api/register")
def api_register():
    data = request.get_json(silent=True) or {}

    nickname = (data.get("nickname") or data.get("apelido") or "").strip()
    phone = (data.get("phone") or data.get("telefone") or "").strip()
    full_name = (data.get("full_name") or data.get("nome_completo") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password")
    confirm = data.get("confirm") or data.get("confirm_password") or data.get("confirmar")

    if not email:
        return json_error("E-mail é obrigatório.", 400)
    if not password:
        return json_error("Senha é obrigatória.", 400)
    if confirm is not None and password != confirm:
        return json_error("As senhas não conferem.", 400)

    ok, reason = validate_password(password)
    if not ok:
        return json_error(reason, 400)

    exists = User.query.filter_by(email=email).first()
    if exists:
        return json_error("E-mail já cadastrado.", 400)

    u = User(
        nickname=nickname or None,
        phone=phone or None,
        full_name=full_name or None,
        email=email,
        password_hash=generate_password_hash(password),
    )
    db.session.add(u)
    db.session.commit()

    session["user_id"] = u.id
    return jsonify({"ok": True, "user": {"id": u.id, "email": u.email}})


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password")

    if not email or not password:
        return json_error("Informe e-mail e senha.", 400)

    u = User.query.filter_by(email=email).first()
    if not u or not check_password_hash(u.password_hash, password):
        return json_error("Credenciais inválidas.", 401)

    session["user_id"] = u.id
    return jsonify({"ok": True, "user": {"id": u.id, "email": u.email}})


@app.post("/api/logout")
def api_logout():
    session.pop("user_id", None)
    return jsonify({"ok": True})


@app.post("/api/reset_password")
def api_reset_password():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password")
    confirm = data.get("confirm")

    if not email or not password:
        return json_error("Informe e-mail e nova senha.", 400)
    if confirm is not None and password != confirm:
        return json_error("As senhas não conferem.", 400)

    ok, reason = validate_password(password)
    if not ok:
        return json_error(reason, 400)

    u = User.query.filter_by(email=email).first()
    if not u:
        return json_error("Usuário não encontrado.", 404)

    u.password_hash = generate_password_hash(password)
    db.session.commit()
    return jsonify({"ok": True})


@app.get("/api/me")
def api_me():
    u = current_user()
    if not u:
        return jsonify({"ok": True, "user": None})
    return jsonify({"ok": True, "user": {"id": u.id, "email": u.email}})


# ---------------------------
# Routes - Transactions
# ---------------------------
@app.post("/api/lancamentos")
@login_required
def api_create_lancamento():
    u = current_user()
    data = request.get_json(silent=True) or {}

    tipo = (data.get("tipo") or "").strip().upper()
    categoria = (data.get("categoria") or "").strip()
    descricao = (data.get("descricao") or "").strip() or None
    origem = (data.get("origem") or "APP").strip().upper()

    # data pode vir "2026-03-01" ou "01/03/2026" do front antigo
    raw_date = (data.get("data") or "").strip()
    if not raw_date:
        return json_error("Data é obrigatória.", 400)

    try:
        if "-" in raw_date:
            dt = parse_date_yyyy_mm_dd(raw_date)
        else:
            dt = datetime.strptime(raw_date, "%d/%m/%Y").date()
    except Exception:
        return json_error("Data inválida. Use YYYY-MM-DD.", 400)

    raw_valor = data.get("valor")
    if raw_valor is None or str(raw_valor).strip() == "":
        return json_error("Valor é obrigatório.", 400)

    # aceita "1000", "1000.50", "1.000,50", "1000,50"
    s = str(raw_valor).strip()
    s = s.replace(".", "").replace(",", ".") if ("," in s) else s
    try:
        valor = float(s)
    except Exception:
        return json_error("Valor inválido.", 400)

    if tipo not in ("RECEITA", "GASTO"):
        return json_error("Tipo inválido. Use RECEITA ou GASTO.", 400)
    if not categoria:
        return json_error("Categoria é obrigatória.", 400)

    t = Transaction(
        user_id=u.id,
        data=dt,
        tipo=tipo,
        categoria=categoria,
        descricao=descricao,
        valor=valor,
        origem=origem,
    )
    db.session.add(t)
    db.session.commit()
    return jsonify({"ok": True, "id": t.id})


@app.get("/api/lancamentos")
@login_required
def api_list_lancamentos():
    """
    Corrige o erro que você mostrou no log:
    AttributeError: 'str' object has no attribute 'isoformat'
    Isso acontece quando 'data' vira string em algum ponto.
    Aqui garantimos serialização consistente.
    """
    u = current_user()
    start = request.args.get("start")  # YYYY-MM-DD
    end = request.args.get("end")      # YYYY-MM-DD

    q = Transaction.query.filter(Transaction.user_id == u.id)

    if start:
        q = q.filter(Transaction.data >= parse_date_yyyy_mm_dd(start))
    if end:
        q = q.filter(Transaction.data <= parse_date_yyyy_mm_dd(end))

    q = q.order_by(Transaction.data.desc(), Transaction.id.desc())

    items = []
    for t in q.limit(300).all():
        dt = t.data
        if isinstance(dt, str):
            # fallback defensivo caso banco/driver devolva string
            try:
                dt = parse_date_yyyy_mm_dd(dt[:10])
            except Exception:
                dt = None

        items.append({
            "id": t.id,
            "data": dt.isoformat() if dt else None,
            "tipo": t.tipo,
            "categoria": t.categoria,
            "descricao": t.descricao,
            "valor": float(t.valor),
            "origem": t.origem,
            "created_at": t.created_at.isoformat() if t.created_at else None
        })

    return jsonify({"ok": True, "items": items})


@app.get("/api/dashboard")
@login_required
def api_dashboard():
    u = current_user()
    mes = request.args.get("mes")  # 1..12
    ano = request.args.get("ano")  # 2026

    try:
        mes = int(mes)
        ano = int(ano)
        if not (1 <= mes <= 12):
            raise ValueError
    except Exception:
        return json_error("Informe mes (1-12) e ano (ex: 2026).", 400)

    start = date(ano, mes, 1)
    if mes == 12:
        end = date(ano + 1, 1, 1)
    else:
        end = date(ano, mes + 1, 1)

    rows = Transaction.query.filter(
        Transaction.user_id == u.id,
        Transaction.data >= start,
        Transaction.data < end,
    ).all()

    receitas = sum(float(r.valor) for r in rows if r.tipo == "RECEITA")
    gastos = sum(float(r.valor) for r in rows if r.tipo == "GASTO")
    saldo = receitas - gastos

    return jsonify({
        "ok": True,
        "mes": mes,
        "ano": ano,
        "receitas": receitas,
        "gastos": gastos,
        "saldo": saldo,
    })


# ---------------------------
# PANIC BUTTON (wipe DB)
# ---------------------------
@app.post("/api/panic/wipe")
@require_panic_token
def api_panic_wipe():
    """
    Apaga TUDO e reseta IDs.
    ATENÇÃO: Isso é irreversível.
    """
    # Ordem não importa com TRUNCATE + CASCADE
    # Inclua aqui qualquer tabela extra que você tenha
    tables = ["transactions", "users", "wa_links", "processed_messages"]

    # Faz TRUNCATE com segurança
    stmt = f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE;"
    db.session.execute(text(stmt))
    db.session.commit()

    # derruba sessão atual também
    session.pop("user_id", None)

    return jsonify({"ok": True, "message": "Banco limpo com sucesso.", "tables": tables})


# ---------------------------
# Init
# ---------------------------
@app.get("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
