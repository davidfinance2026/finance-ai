import os
import re
import json
import time
import datetime as dt
from dateutil import parser as dateparser

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import jwt

# ----------------------------
# Config / App
# ----------------------------
db = SQLAlchemy()

def _normalize_database_url(url: str | None) -> str | None:
    if not url:
        return None
    # Railway às vezes fornece "postgres://", SQLAlchemy quer "postgresql://"
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url

def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app)

    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")

    database_url = _normalize_database_url(os.getenv("DATABASE_URL"))
    if not database_url:
        # fallback local
        database_url = "sqlite:///local.db"

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    with app.app_context():
        db.create_all()

    return app

app = create_app()

# ----------------------------
# Models
# ----------------------------
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)

    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)

    created_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)

class Transaction(db.Model):
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    # Padronizei como "date" (não "data") pra evitar o erro que você teve
    date = db.Column(db.Date, nullable=False, index=True)

    # "RECEITA" ou "GASTO"
    type = db.Column(db.String(20), nullable=False, index=True)

    category = db.Column(db.String(120), nullable=False, default="Outros")
    description = db.Column(db.String(255), nullable=True)

    # valor em centavos (evita bug 360 -> 360000)
    value_cents = db.Column(db.Integer, nullable=False)

    origin = db.Column(db.String(30), nullable=False, default="APP")  # APP / WA / SHEETS
    created_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)

class WaLink(db.Model):
    __tablename__ = "wa_links"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    wa_phone_number_id = db.Column(db.String(80), nullable=True)
    wa_business_account_id = db.Column(db.String(80), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)

# ----------------------------
# Helpers
# ----------------------------
def _json_error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status

def _make_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "iat": int(time.time()),
        "exp": int(time.time()) + 60 * 60 * 24 * 30,  # 30 dias
    }
    return jwt.encode(payload, app.config["SECRET_KEY"], algorithm="HS256")

def _get_bearer_user_id() -> int | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.replace("Bearer ", "", 1).strip()
    try:
        payload = jwt.decode(token, app.config["SECRET_KEY"], algorithms=["HS256"])
        return int(payload["sub"])
    except Exception:
        return None

def _parse_money_to_cents(value) -> int:
    """
    Aceita:
      360
      "360"
      "360,00"
      "360.00"
      "R$ 360,00"
      "1.234,56"
    Retorna centavos (int).
    """
    if value is None:
        raise ValueError("Valor ausente")

    if isinstance(value, (int, float)):
        # 360.0 -> 36000
        return int(round(float(value) * 100))

    s = str(value).strip()
    s = s.replace("R$", "").replace(" ", "")

    # Se tem vírgula e ponto, assume pt-BR: 1.234,56
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        # Se só tem vírgula: 360,00
        if "," in s:
            s = s.replace(",", ".")
        # Se só tem ponto: já ok

    # Mantém só números e ponto
    s = re.sub(r"[^0-9.]", "", s)
    if s.count(".") > 1:
        # algo estranho, remove todos os pontos e deixa o último como decimal
        parts = s.split(".")
        s = "".join(parts[:-1]) + "." + parts[-1]

    amount = float(s) if s else 0.0
    return int(round(amount * 100))

def _parse_date(d) -> dt.date:
    """
    Aceita:
      "01/03/2026"
      "2026-03-01"
      datetime/date
    """
    if isinstance(d, dt.date) and not isinstance(d, dt.datetime):
        return d
    if isinstance(d, dt.datetime):
        return d.date()
    if not d:
        return dt.date.today()

    s = str(d).strip()
    # Força pt-BR dd/mm/yyyy quando tem "/"
    if "/" in s:
        day, month, year = s.split("/")
        return dt.date(int(year), int(month), int(day))

    # fallback ISO/geral
    return dateparser.parse(s).date()

# ----------------------------
# Health / Root
# ----------------------------
@app.get("/")
def root():
    return "Finance AI 🚀\n\nBackend funcionando corretamente.\n", 200

@app.get("/health")
def health():
    # Testa conexão rápida com banco
    try:
        db.session.execute(db.text("SELECT 1"))
        return jsonify({"ok": True, "db": True})
    except Exception as e:
        return jsonify({"ok": True, "db": False, "detail": str(e)}), 200

# ----------------------------
# Auth
# ----------------------------
@app.post("/api/register")
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return _json_error("Email e senha são obrigatórios.", 400)

    if User.query.filter_by(email=email).first():
        return _json_error("Esse email já está cadastrado.", 409)

    u = User(email=email, password_hash=generate_password_hash(password))
    db.session.add(u)
    db.session.commit()

    token = _make_token(u.id)
    return jsonify({"ok": True, "token": token, "user": {"id": u.id, "email": u.email}})

@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    u = User.query.filter_by(email=email).first()
    if not u or not check_password_hash(u.password_hash, password):
        return _json_error("Credenciais inválidas.", 401)

    token = _make_token(u.id)
    return jsonify({"ok": True, "token": token, "user": {"id": u.id, "email": u.email}})

@app.get("/api/me")
def me():
    user_id = _get_bearer_user_id()
    if not user_id:
        return _json_error("Não autorizado.", 401)
    u = User.query.get(user_id)
    if not u:
        return _json_error("Usuário não encontrado.", 404)
    return jsonify({"ok": True, "user": {"id": u.id, "email": u.email}})

# ----------------------------
# Transactions (Lançamentos)
# ----------------------------
@app.get("/api/lancamentos")
def list_lancamentos():
    user_id = _get_bearer_user_id()
    if not user_id:
        return _json_error("Não autorizado.", 401)

    limit = int(request.args.get("limit", "30"))
    q = (
        Transaction.query
        .filter_by(user_id=user_id)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(limit)
        .all()
    )

    def to_dict(t: Transaction):
        return {
            "id": t.id,
            "date": t.date.isoformat(),
            "type": t.type,
            "category": t.category,
            "description": t.description,
            "value": round(t.value_cents / 100.0, 2),
            "value_cents": t.value_cents,
            "origin": t.origin,
            "created_at": t.created_at.isoformat(),
        }

    return jsonify({"ok": True, "items": [to_dict(x) for x in q]})

@app.post("/api/lancamentos")
def create_lancamento():
    user_id = _get_bearer_user_id()
    if not user_id:
        return _json_error("Não autorizado.", 401)

    data = request.get_json(silent=True) or {}
    tx_type = (data.get("tipo") or data.get("type") or "").strip().upper()
    if tx_type not in ("RECEITA", "GASTO"):
        return _json_error("Tipo inválido (use RECEITA ou GASTO).", 400)

    try:
        date = _parse_date(data.get("data") or data.get("date"))
        value_cents = _parse_money_to_cents(data.get("valor") or data.get("value"))
    except Exception as e:
        return _json_error(f"Dados inválidos: {e}", 400)

    category = (data.get("categoria") or data.get("category") or "Outros").strip()
    description = (data.get("descricao") or data.get("description") or "").strip() or None
    origin = (data.get("origem") or data.get("origin") or "APP").strip().upper()

    t = Transaction(
        user_id=user_id,
        date=date,
        type=tx_type,
        category=category,
        description=description,
        value_cents=value_cents,
        origin=origin,
    )
    db.session.add(t)
    db.session.commit()

    return jsonify({"ok": True, "id": t.id})

@app.delete("/api/lancamentos/<int:tx_id>")
def delete_lancamento(tx_id: int):
    user_id = _get_bearer_user_id()
    if not user_id:
        return _json_error("Não autorizado.", 401)

    t = Transaction.query.filter_by(id=tx_id, user_id=user_id).first()
    if not t:
        return _json_error("Lançamento não encontrado.", 404)

    db.session.delete(t)
    db.session.commit()
    return jsonify({"ok": True})

# ----------------------------
# Dashboard
# ----------------------------
@app.get("/api/dashboard")
def dashboard():
    user_id = _get_bearer_user_id()
    if not user_id:
        return _json_error("Não autorizado.", 401)

    month = int(request.args.get("month", dt.date.today().month))
    year = int(request.args.get("year", dt.date.today().year))

    start = dt.date(year, month, 1)
    # próximo mês
    if month == 12:
        end = dt.date(year + 1, 1, 1)
    else:
        end = dt.date(year, month + 1, 1)

    rows = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
        .filter(Transaction.date >= start)
        .filter(Transaction.date < end)
        .all()
    )

    receitas = sum(t.value_cents for t in rows if t.type == "RECEITA")
    gastos = sum(t.value_cents for t in rows if t.type == "GASTO")
    saldo = receitas - gastos

    return jsonify({
        "ok": True,
        "month": month,
        "year": year,
        "receitas": round(receitas / 100.0, 2),
        "gastos": round(gastos / 100.0, 2),
        "saldo": round(saldo / 100.0, 2),
    })

# ----------------------------
# WhatsApp Webhook (Meta)
# ----------------------------
@app.get("/webhooks/whatsapp")
def wa_verify():
    verify_token = os.getenv("WA_VERIFY_TOKEN", "")
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == verify_token and challenge:
        return challenge, 200
    return "forbidden", 403

@app.post("/webhooks/whatsapp")
def wa_incoming():
    # Por enquanto apenas confirma recebimento (200) pra não quebrar o webhook
    # Depois a gente implementa parse do texto e grava Transaction com origin="WA"
    data = request.get_json(silent=True) or {}
    debug = os.getenv("DEBUG_LOG_PAYLOAD", "0") == "1"
    if debug:
        print("WA payload:", json.dumps(data)[:4000])

    return jsonify({"ok": True}), 200

# ----------------------------
# Error handler (pra não virar 500 "mudo")
# ----------------------------
@app.errorhandler(Exception)
def handle_exception(e):
    # Loga no Railway
    print("Unhandled error:", repr(e))
    return jsonify({"ok": False, "error": "Erro interno", "detail": str(e)}), 500
