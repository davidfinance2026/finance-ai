import os
import re
import json
import datetime as dt
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy


# ----------------------------
# Helpers
# ----------------------------
def normalize_database_url(url: str) -> str:
    """
    Railway às vezes entrega postgres://...
    SQLAlchemy prefere postgresql+psycopg2://...
    """
    if not url:
        return ""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def parse_brl_number(text: str) -> Decimal:
    """
    Aceita:
      "45", "45.00", "45,00", "1.234,56", "1234,56"
    Retorna Decimal.
    """
    s = (text or "").strip()
    if not s:
        raise InvalidOperation("empty")

    # remove moeda e espaços
    s = re.sub(r"[^\d,.\-]", "", s)

    # se tiver vírgula e ponto, assume formato BR (1.234,56)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    # se só tiver vírgula, troca por ponto
    elif "," in s:
        s = s.replace(",", ".")
    return Decimal(s)


def parse_iso_or_br_date(text: str) -> dt.date:
    """
    Aceita "2026-03-01" ou "01/03/2026"
    """
    t = (text or "").strip()
    if not t:
        return dt.date.today()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", t):
        return dt.date.fromisoformat(t)
    if re.match(r"^\d{2}/\d{2}/\d{4}$", t):
        d, m, y = t.split("/")
        return dt.date(int(y), int(m), int(d))
    # fallback
    return dt.date.today()


def require_env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v


# ----------------------------
# Flask App
# ----------------------------
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}, r"/webhooks/*": {"origins": "*"}})

app.config["SECRET_KEY"] = require_env("SECRET_KEY", "dev-secret")
db_url = normalize_database_url(require_env("DATABASE_URL", ""))
app.config["SQLALCHEMY_DATABASE_URI"] = db_url if db_url else "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# ----------------------------
# Models
# ----------------------------
class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)


class Transaction(db.Model):
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    date = db.Column(db.Date, nullable=False, default=dt.date.today)
    tipo = db.Column(db.String(20), nullable=False)  # "RECEITA" | "GASTO"
    categoria = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.String(255), nullable=True)

    valor = db.Column(db.Numeric(14, 2), nullable=False)  # 2 casas
    origem = db.Column(db.String(30), nullable=False, default="APP")  # APP | WA | etc

    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)


class WaLink(db.Model):
    __tablename__ = "wa_links"
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(40), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow, nullable=False)


# cria tabelas no boot (evita “não apareceu tabelas” depois de recriar Postgres)
with app.app_context():
    db.create_all()


# ----------------------------
# Health / Root
# ----------------------------
@app.get("/")
def root():
    return "Finance AI 🚀 Backend funcionando corretamente.", 200


@app.get("/health")
def health():
    return jsonify({"ok": True}), 200


# ----------------------------
# Auth (simples)
# ----------------------------
@app.post("/api/register")
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()

    if not email or not password:
        return jsonify({"error": "email e password são obrigatórios"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "email já cadastrado"}), 409

    u = User(email=email, password=password)
    db.session.add(u)
    db.session.commit()

    return jsonify({"ok": True, "user_id": u.id, "email": u.email}), 201


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()

    u = User.query.filter_by(email=email).first()
    if not u or u.password != password:
        return jsonify({"error": "credenciais inválidas"}), 401

    # Token simples só pra destravar o front (em produção, use JWT)
    token = f"u:{u.id}:{app.config['SECRET_KEY']}"
    return jsonify({"ok": True, "token": token, "user_id": u.id, "email": u.email}), 200


def get_user_id_from_auth() -> int | None:
    """
    Espera header Authorization: Bearer u:<id>:<secret>
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.replace("Bearer ", "", 1).strip()
    parts = token.split(":")
    if len(parts) < 3:
        return None
    if parts[0] != "u":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


# ----------------------------
# Transactions API
# ----------------------------
@app.get("/api/lancamentos")
def list_lancamentos():
    user_id = get_user_id_from_auth()
    if not user_id:
        return jsonify({"error": "Você precisa estar logado."}), 401

    limit = int(request.args.get("limit", "30"))
    q = (
        Transaction.query
        .filter_by(user_id=user_id)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(limit)
        .all()
    )

    out = []
    for t in q:
        out.append({
            "id": t.id,
            "date": t.date.isoformat(),
            "tipo": t.tipo,
            "categoria": t.categoria,
            "descricao": t.descricao,
            "valor": float(t.valor),
            "origem": t.origem,
            "created_at": t.created_at.isoformat()
        })
    return jsonify({"items": out}), 200


@app.post("/api/lancamentos")
def create_lancamento():
    user_id = get_user_id_from_auth()
    if not user_id:
        return jsonify({"error": "Você precisa estar logado."}), 401

    data = request.get_json(silent=True) or {}
    tipo = (data.get("tipo") or "").strip().upper()
    categoria = (data.get("categoria") or "").strip()
    descricao = (data.get("descricao") or "").strip() or None
    origem = (data.get("origem") or "APP").strip().upper()
    date = parse_iso_or_br_date(data.get("date") or data.get("data") or "")

    if tipo not in ("RECEITA", "GASTO"):
        return jsonify({"error": "tipo deve ser RECEITA ou GASTO"}), 400
    if not categoria:
        return jsonify({"error": "categoria é obrigatória"}), 400

    try:
        valor = parse_brl_number(str(data.get("valor") or data.get("value") or ""))
    except Exception:
        return jsonify({"error": "valor inválido"}), 400

    t = Transaction(
        user_id=user_id,
        date=date,
        tipo=tipo,
        categoria=categoria,
        descricao=descricao,
        valor=valor.quantize(Decimal("0.01")),
        origem=origem,
    )
    db.session.add(t)
    db.session.commit()

    return jsonify({"ok": True, "id": t.id}), 201


@app.get("/api/dashboard")
def dashboard():
    user_id = get_user_id_from_auth()
    if not user_id:
        return jsonify({"error": "Você precisa estar logado."}), 401

    mes = int(request.args.get("mes", dt.date.today().month))
    ano = int(request.args.get("ano", dt.date.today().year))

    start = dt.date(ano, mes, 1)
    end = dt.date(ano + (1 if mes == 12 else 0), 1 if mes == 12 else (mes + 1), 1)

    rows = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
        .filter(Transaction.date >= start)
        .filter(Transaction.date < end)
        .all()
    )

    receitas = Decimal("0.00")
    gastos = Decimal("0.00")
    for r in rows:
        if r.tipo == "RECEITA":
            receitas += Decimal(r.valor)
        else:
            gastos += Decimal(r.valor)

    saldo = receitas - gastos
    return jsonify({
        "mes": mes,
        "ano": ano,
        "receitas": float(receitas),
        "gastos": float(gastos),
        "saldo": float(saldo),
    }), 200


# ----------------------------
# WhatsApp Webhook (Cloud API)
# ----------------------------
WA_VERIFY_TOKEN = require_env("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN = require_env("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = require_env("WA_PHONE_NUMBER_ID", "")
META_APP_SECRET = require_env("META_APP_SECRET", "")


@app.get("/webhooks/whatsapp")
def wa_verify():
    """
    Verificação do webhook (GET) no Meta.
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token and token == WA_VERIFY_TOKEN:
        return challenge or "", 200
    return "Forbidden", 403


def wa_send_message(to_phone: str, text: str) -> None:
    """
    Envia mensagem via WhatsApp Cloud API.
    """
    if not (WA_ACCESS_TOKEN and WA_PHONE_NUMBER_ID):
        return

    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }
    try:
        requests.post(url, headers=headers, json=payload, timeout=15)
    except Exception:
        pass


def parse_wa_command(text: str):
    """
    Aceita mensagens tipo:
      "gasto 32,90 mercado"
      "salário 100"
      "receita 1000 salario"
    Retorna dict com tipo/categoria/valor/descricao
    """
    t = (text or "").strip()
    if not t:
        return None

    lower = t.lower()

    # padrão: "gasto 32,90 mercado"
    m = re.match(r"^(gasto|despesa|receita|salario|salário)\s+([\d.,]+)\s*(.*)$", lower, re.IGNORECASE)
    if not m:
        return None

    kind = m.group(1)
    val_s = m.group(2)
    rest = (m.group(3) or "").strip()

    tipo = "GASTO" if kind in ("gasto", "despesa") else "RECEITA"
    categoria = "Mercado" if "mercado" in rest else (rest.title() if rest else ("Salário" if "sal" in kind else "Geral"))
    descricao = None

    try:
        valor = parse_brl_number(val_s).quantize(Decimal("0.01"))
    except Exception:
        return None

    return {"tipo": tipo, "categoria": categoria, "descricao": descricao, "valor": valor}


@app.post("/webhooks/whatsapp")
def wa_webhook():
    """
    Recebe mensagens e grava no banco quando houver link phone->user.
    """
    payload = request.get_json(silent=True) or {}

    # extrai texto do webhook (estrutura padrão)
    try:
        entry = (payload.get("entry") or [])[0]
        changes = (entry.get("changes") or [])[0]
        value = changes.get("value") or {}
        messages = value.get("messages") or []
        if not messages:
            return "OK", 200

        msg = messages[0]
        from_phone = msg.get("from")  # ex: "5531..."
        text = (msg.get("text") or {}).get("body") or ""
    except Exception:
        return "OK", 200

    # encontra vínculo
    link = WaLink.query.filter_by(phone=from_phone).order_by(WaLink.id.desc()).first()
    if not link:
        wa_send_message(from_phone, "❗Seu WhatsApp ainda não está vinculado a um usuário do app.")
        return "OK", 200

    cmd = parse_wa_command(text)
    if not cmd:
        wa_send_message(from_phone, "Envie assim: gasto 32,90 mercado  |  receita 1000 salario")
        return "OK", 200

    t = Transaction(
        user_id=link.user_id,
        date=dt.date.today(),
        tipo=cmd["tipo"],
        categoria=cmd["categoria"],
        descricao=cmd["descricao"],
        valor=cmd["valor"],
        origem="WA",
    )
    db.session.add(t)
    db.session.commit()

    wa_send_message(
        from_phone,
        f"✅ Lançamento salvo!\nTipo: {t.tipo}\nValor: R$ {t.valor}\nCategoria: {t.categoria}\nData: {t.date.isoformat()}"
    )
    return "OK", 200


# ----------------------------
# Run local
# ----------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
