# -*- coding: utf-8 -*-
import os
import re
import json
import hashlib
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask, request, jsonify, send_from_directory, session, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, func


# -------------------------
# App / Config
# -------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
app.config["JSON_AS_ASCII"] = False

# Cookies de sessão (Railway/HTTPS)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = os.getenv("SESSION_SAMESITE", "Lax")
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_SECURE", "1") == "1"

# DB
_raw_db_url = os.getenv("DATABASE_URL", "").strip()
if _raw_db_url.startswith("postgres://"):
    _raw_db_url = _raw_db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = _raw_db_url or ("sqlite:///" + os.path.join(BASE_DIR, "local.db"))
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
    "pool_size": int(os.getenv("DB_POOL_SIZE", "3")),
    "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "2")),
}

DB_ENABLED = bool(_raw_db_url)

# Senha mínima (ALINHADO com seu front: "mínimo 6")
MIN_PASSWORD_LEN = int(os.getenv("MIN_PASSWORD_LEN", "6"))

# WhatsApp Cloud API (Meta)
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "").strip()
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "").strip()
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "").strip()
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v20.0").strip()

# Botão de pânico (token opcional)
PANIC_TOKEN = os.getenv("PANIC_TOKEN", "").strip()


# -------------------------
# DB
# -------------------------
db = SQLAlchemy(app)


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(64), nullable=False)
    password_set = db.Column(db.Boolean, nullable=False, server_default=text("false"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Transaction(db.Model):
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    tipo = db.Column(db.String(16), nullable=False)  # RECEITA/GASTO
    data = db.Column(db.Date, nullable=False, index=True)  # coluna "data"
    categoria = db.Column(db.String(80), nullable=False, index=True)
    descricao = db.Column(db.Text, nullable=True)
    valor = db.Column(db.Numeric(12, 2), nullable=False)
    origem = db.Column(db.String(16), nullable=False, default="APP")  # APP/WA
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class WaLink(db.Model):
    __tablename__ = "wa_links"
    id = db.Column(db.Integer, primary_key=True)
    wa_from = db.Column(db.String(40), unique=True, nullable=False, index=True)  # número sem +
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ProcessedMessage(db.Model):
    __tablename__ = "processed_messages"
    id = db.Column(db.Integer, primary_key=True)
    msg_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    wa_from = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class CategoryRule(db.Model):
    """
    Regras personalizadas por usuário para categorias no WhatsApp.
    Ex: pattern="ifood" => categoria="Alimentação"
    """
    __tablename__ = "category_rules"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    pattern = db.Column(db.String(80), nullable=False)      # keyword simples
    categoria = db.Column(db.String(80), nullable=False)
    priority = db.Column(db.Integer, nullable=False, server_default=text("10"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class WaPending(db.Model):
    """
    Guarda pendências do WhatsApp (modo dúvida / confirmação).
    Ex: mensagem ambígua -> pergunta "receita ou gasto?" e salva aqui para completar depois.
    """
    __tablename__ = "wa_pending"
    id = db.Column(db.Integer, primary_key=True)
    wa_from = db.Column(db.String(40), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    kind = db.Column(db.String(40), nullable=False)  # ex: "CONFIRM_TIPO"
    payload_json = db.Column(db.Text, nullable=False)  # json do lançamento pendente
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class RecurringRule(db.Model):
    """Regras de recorrência (MONTHLY/WEEKLY/DAILY)."""
    __tablename__ = "recurring_rules"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    active = db.Column(db.Boolean, nullable=False, server_default=text("true"))

    tipo = db.Column(db.String(16), nullable=False)  # RECEITA/GASTO
    categoria = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    valor = db.Column(db.Numeric(12, 2), nullable=False)

    frequency = db.Column(db.String(16), nullable=False)  # MONTHLY | WEEKLY | DAILY
    day_of_month = db.Column(db.Integer, nullable=True)   # 1..28 (MONTHLY)
    day_of_week = db.Column(db.Integer, nullable=True)    # 0..6 (WEEKLY)

    start_date = db.Column(db.Date, nullable=False, default=date.today)
    next_run = db.Column(db.Date, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


def _create_tables_if_needed():
    try:
        db.create_all()
    except Exception as e:
        print("DB create_all failed:", repr(e))


with app.app_context():
    _create_tables_if_needed()


# -------------------------
# Helpers
# -------------------------
def _hash_password(pw: str) -> str:
    return hashlib.sha256((pw or "").encode("utf-8")).hexdigest()


def _normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def _get_logged_user_id():
    return session.get("user_id")


def _get_logged_email():
    return session.get("user_email")


def _require_login():
    return _get_logged_user_id()


def _parse_brl_value(v) -> Decimal:
    if v is None:
        raise ValueError("valor vazio")
    s = str(v).strip()
    if not s:
        raise ValueError("valor vazio")

    s = re.sub(r"[^0-9,\.-]", "", s)

    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        if "," in s and "." not in s:
            s = s.replace(",", ".")

    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError("valor inválido")


def _parse_date_any(v) -> date:
    if not v:
        return datetime.utcnow().date()
    s = str(v).strip()
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return datetime.strptime(s, "%Y-%m-%d").date()
        if re.match(r"^\d{2}/\d{2}/\d{4}$", s):
            return datetime.strptime(s, "%d/%m/%Y").date()
    except Exception:
        pass
    return datetime.utcnow().date()


def _get_or_create_user_by_email(email: str, password: str | None = None) -> User:
    email = _normalize_email(email)
    u = User.query.filter_by(email=email).first()
    if u:
        return u

    if password is None:
        pw_hash = _hash_password(os.urandom(16).hex())
        u = User(email=email, password_hash=pw_hash, password_set=False)
    else:
        u = User(email=email, password_hash=_hash_password(password), password_set=True)

    db.session.add(u)
    db.session.commit()
    return u


def _login_user(u: User):
    session["user_id"] = u.id
    session["user_email"] = u.email


def _status_payload():
    return {
        "ok": True,
        "db_enabled": DB_ENABLED,
        "db_uri_set": bool(_raw_db_url),
        "graph_version": GRAPH_VERSION,
        "wa_ready": bool(WA_ACCESS_TOKEN and WA_PHONE_NUMBER_ID and WA_VERIFY_TOKEN),
        "min_password_len": MIN_PASSWORD_LEN,
    }


def _panic_allowed() -> bool:
    if not PANIC_TOKEN:
        return True
    token = (
        request.headers.get("X-Panic-Token")
        or request.args.get("token")
        or (request.get_json(silent=True) or {}).get("token")
        or ""
    )
    return str(token) == PANIC_TOKEN


def _month_range(ano: int, mes: int):
    start = date(ano, mes, 1)
    end = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)
    return start, end


def _safe_title(s: str) -> str:
    return (str(s or "").strip().title() or "Outros")[:80]


def _format_brl(v: Decimal | float | int) -> str:
    try:
        if isinstance(v, Decimal):
            s = f"{v:.2f}"
        else:
            s = f"{Decimal(str(v)):.2f}"
        return s.replace(".", ",")
    except Exception:
        return str(v)


# -------------------------
# WhatsApp send (Meta Cloud API)
# -------------------------
def _normalize_wa_number(raw: str) -> str:
    s = (raw or "").strip().replace("+", "")
    s = re.sub(r"[^0-9]", "", s)
    return s


def wa_send_text(to_number: str, text_msg: str):
    to_number = _normalize_wa_number(to_number)
    if not (WA_PHONE_NUMBER_ID and WA_ACCESS_TOKEN and to_number):
        print("WA send skipped (missing creds or number). msg:", text_msg)
        return

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": str(text_msg or "")[:3900]},
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code >= 400:
            print("WA send error:", r.status_code, r.text)
    except Exception as e:
        print("WA send exception:", repr(e))


# -------------------------
# Static / Frontend
# -------------------------
@app.get("/")
def home():
    return render_template("index.html")


@app.get("/offline.html")
def offline_page():
    return render_template("offline.html")


@app.get("/manifest.json")
def manifest():
    return send_from_directory(app.static_folder, "manifest.json")


@app.get("/sw.js")
def service_worker():
    resp = send_from_directory(app.static_folder, "sw.js")
    resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    return resp


@app.get("/robots.txt")
def robots():
    return send_from_directory(app.static_folder, "robots.txt")


@app.get("/health")
def health():
    return jsonify(_status_payload())


# -------------------------
# Panic Reset (limpa tudo)
# -------------------------
@app.route("/api/panic_reset", methods=["GET", "POST"])
def api_panic_reset():
    if not _panic_allowed():
        return jsonify({"error": "forbidden"}), 403

    try:
        db.session.execute(
            text(
                "TRUNCATE TABLE processed_messages, wa_links, transactions, category_rules, wa_pending, recurring_rules, users "
                "RESTART IDENTITY CASCADE;"
            )
        )
        db.session.commit()
        return jsonify({"ok": True, "message": "Banco limpo."})
    except Exception:
        db.session.rollback()

    try:
        ProcessedMessage.query.delete()
        WaLink.query.delete()
        Transaction.query.delete()
        CategoryRule.query.delete()
        WaPending.query.delete()
        RecurringRule.query.delete()
        User.query.delete()
        db.session.commit()
        return jsonify({"ok": True, "message": "Banco limpo (fallback)."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "panic_reset_failed", "detail": str(e)}), 500


# -------------------------
# Auth API (ALINHADO COM SEU index.html)
# -------------------------
@app.post("/api/register")
def api_register():
    data = request.get_json(silent=True) or {}

    email = _normalize_email(data.get("email"))
    senha = str(data.get("senha") or data.get("password") or "")
    confirmar = str(data.get("confirmar_senha") or data.get("confirmar") or data.get("confirm") or "")

    if not email or "@" not in email:
        return jsonify(error="Email inválido"), 400
    if len(senha) < MIN_PASSWORD_LEN:
        return jsonify(error=f"Senha deve ter pelo menos {MIN_PASSWORD_LEN} caracteres"), 400
    if senha != confirmar:
        return jsonify(error="Senhas não conferem"), 400

    existing = User.query.filter_by(email=email).first()
    if existing:
        if getattr(existing, "password_set", False) is False:
            existing.password_hash = _hash_password(senha)
            existing.password_set = True
            db.session.commit()
            _login_user(existing)
            return jsonify(email=existing.email, claimed=True)
        return jsonify(error="Email já cadastrado"), 400

    u = _get_or_create_user_by_email(email, password=senha)
    _login_user(u)
    return jsonify(email=u.email)


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    email = _normalize_email(data.get("email"))
    senha = str(data.get("senha") or data.get("password") or "")

    u = User.query.filter_by(email=email).first()
    if not u or u.password_hash != _hash_password(senha):
        return jsonify(error="Email ou senha inválidos"), 401

    _login_user(u)
    return jsonify(email=u.email)


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/reset_password")
def api_reset_password():
    data = request.get_json(silent=True) or {}
    email = _normalize_email(data.get("email"))
    nova = str(data.get("nova_senha") or data.get("newPassword") or data.get("password") or "")
    confirmar = str(data.get("confirmar") or data.get("confirm") or "")

    if not email or "@" not in email:
        return jsonify(error="Email inválido"), 400
    if len(nova) < MIN_PASSWORD_LEN:
        return jsonify(error=f"Senha deve ter pelo menos {MIN_PASSWORD_LEN} caracteres"), 400
    if nova != confirmar:
        return jsonify(error="Senhas não conferem"), 400

    u = User.query.filter_by(email=email).first()
    if not u:
        return jsonify(error="Email não encontrado"), 404

    u.password_hash = _hash_password(nova)
    u.password_set = True
    db.session.commit()
    return jsonify(ok=True)


@app.get("/api/me")
def api_me():
    return jsonify(email=_get_logged_email(), user_id=_get_logged_user_id())


# -------------------------
# Transactions API
# -------------------------
@app.get("/api/lancamentos")
def api_list_lancamentos():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    limit = int(request.args.get("limit", 30))
    limit = max(1, min(limit, 200))

    rows = (
        Transaction.query
        .filter(Transaction.user_id == uid)
        .order_by(Transaction.data.desc(), Transaction.id.desc())
        .limit(limit)
        .all()
    )

    items = []
    for t in rows:
        items.append(
            {
                "row": t.id,
                "id": t.id,
                "data": t.data.isoformat() if t.data else None,
                "tipo": t.tipo,
                "categoria": t.categoria,
                "descricao": t.descricao or "",
                "valor": float(t.valor) if t.valor is not None else 0.0,
                "origem": t.origem,
                "criado_em": t.created_at.isoformat() if t.created_at else "",
            }
        )

    return jsonify(items=items)


@app.post("/api/lancamentos")
def api_create_lancamento():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    dataj = request.get_json(silent=True) or {}

    tipo = str(dataj.get("tipo") or "").strip().upper()
    if tipo not in ("RECEITA", "GASTO"):
        return jsonify(error="Tipo inválido"), 400

    categoria = _safe_title(dataj.get("categoria") or "Outros")
    descricao = str(dataj.get("descricao") or "").strip() or None
    d = _parse_date_any(dataj.get("data"))

    try:
        valor = _parse_brl_value(dataj.get("valor"))
    except ValueError as e:
        return jsonify(error=str(e)), 400

    t = Transaction(
        user_id=uid,
        tipo=tipo,
        data=d,
        categoria=categoria,
        descricao=descricao,
        valor=valor,
        origem="APP",
    )
    db.session.add(t)
    db.session.commit()
    return jsonify(ok=True, id=t.id, row=t.id)


@app.put("/api/lancamentos/<int:row>")
def api_edit_lancamento(row: int):
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    payload = request.get_json(silent=True) or {}

    t = Transaction.query.filter_by(id=row, user_id=uid).first()
    if not t:
        return jsonify(error="Sem permissão ou inexistente"), 403

    tipo = str(payload.get("tipo") or t.tipo).strip().upper()
    if tipo not in ("RECEITA", "GASTO"):
        return jsonify(error="Tipo inválido"), 400

    t.tipo = tipo
    t.data = _parse_date_any(payload.get("data") or t.data.isoformat())
    t.categoria = _safe_title(payload.get("categoria") or t.categoria)
    t.descricao = str(payload.get("descricao") or "").strip() or None

    try:
        t.valor = _parse_brl_value(payload.get("valor"))
    except ValueError as e:
        return jsonify(error=str(e)), 400

    db.session.commit()
    return jsonify(ok=True)


@app.delete("/api/lancamentos/<int:row>")
def api_delete_lancamento(row: int):
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    t = Transaction.query.filter_by(id=row, user_id=uid).first()
    if not t:
        return jsonify(error="Sem permissão ou inexistente"), 403

    db.session.delete(t)
    db.session.commit()
    return jsonify(ok=True)


@app.get("/api/dashboard")
def api_dashboard():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    try:
        mes = int(request.args.get("mes"))
        ano = int(request.args.get("ano"))
    except Exception:
        return jsonify(error="Parâmetros mes/ano inválidos"), 400

    start, end = _month_range(ano, mes)

    q = (
        Transaction.query
        .filter(Transaction.user_id == uid)
        .filter(Transaction.data >= start)
        .filter(Transaction.data < end)
        .all()
    )

    receitas = Decimal("0")
    gastos = Decimal("0")
    for t in q:
        v = Decimal(t.valor or 0)
        if (t.tipo or "").upper() == "RECEITA":
            receitas += v
        else:
            gastos += v

    saldo = receitas - gastos
    return jsonify(receitas=float(receitas), gastos=float(gastos), saldo=float(saldo))


# -------------------------
# A) Consolidados + Resumos inteligentes
# -------------------------
@app.get("/api/consolidados")
def api_consolidados():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    try:
        mes = int(request.args.get("mes"))
        ano = int(request.args.get("ano"))
    except Exception:
        return jsonify(error="Parâmetros mes/ano inválidos"), 400

    start, end = _month_range(ano, mes)

    rows_tipo = (
        db.session.query(Transaction.tipo, func.coalesce(func.sum(Transaction.valor), 0))
        .filter(Transaction.user_id == uid)
        .filter(Transaction.data >= start, Transaction.data < end)
        .group_by(Transaction.tipo)
        .all()
    )
    receitas = Decimal("0")
    gastos = Decimal("0")
    for tipo, total in rows_tipo:
        if (tipo or "").upper() == "RECEITA":
            receitas += Decimal(total or 0)
        else:
            gastos += Decimal(total or 0)

    gastos_cat = (
        db.session.query(Transaction.categoria, func.coalesce(func.sum(Transaction.valor), 0))
        .filter(Transaction.user_id == uid, Transaction.tipo == "GASTO")
        .filter(Transaction.data >= start, Transaction.data < end)
        .group_by(Transaction.categoria)
        .order_by(func.sum(Transaction.valor).desc())
        .limit(12)
        .all()
    )
    receitas_cat = (
        db.session.query(Transaction.categoria, func.coalesce(func.sum(Transaction.valor), 0))
        .filter(Transaction.user_id == uid, Transaction.tipo == "RECEITA")
        .filter(Transaction.data >= start, Transaction.data < end)
        .group_by(Transaction.categoria)
        .order_by(func.sum(Transaction.valor).desc())
        .limit(12)
        .all()
    )

    serie = (
        db.session.query(Transaction.data, Transaction.tipo, func.coalesce(func.sum(Transaction.valor), 0))
        .filter(Transaction.user_id == uid)
        .filter(Transaction.data >= start, Transaction.data < end)
        .group_by(Transaction.data, Transaction.tipo)
        .order_by(Transaction.data.asc())
        .all()
    )

    by_day = {}
    for d, tipo, total in serie:
        key = d.isoformat()
        if key not in by_day:
            by_day[key] = {"data": key, "receitas": 0.0, "gastos": 0.0}
        if (tipo or "").upper() == "RECEITA":
            by_day[key]["receitas"] = float(total or 0)
        else:
            by_day[key]["gastos"] = float(total or 0)

    saldo = receitas - gastos
    return jsonify(
        mes=mes,
        ano=ano,
        totais={"receitas": float(receitas), "gastos": float(gastos), "saldo": float(saldo)},
        gastos_por_categoria=[{"categoria": c, "total": float(v)} for c, v in gastos_cat],
        receitas_por_categoria=[{"categoria": c, "total": float(v)} for c, v in receitas_cat],
        serie_diaria=list(by_day.values()),
    )


@app.get("/api/resumos_inteligentes")
def api_resumos_inteligentes():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    try:
        mes = int(request.args.get("mes"))
        ano = int(request.args.get("ano"))
    except Exception:
        return jsonify(error="Parâmetros mes/ano inválidos"), 400

    start, end = _month_range(ano, mes)
    today = datetime.utcnow().date()
    effective_end = min(end, today + timedelta(days=1))

    def totals_for(s: date, e: date):
        rows = (
            db.session.query(Transaction.tipo, func.coalesce(func.sum(Transaction.valor), 0))
            .filter(Transaction.user_id == uid)
            .filter(Transaction.data >= s, Transaction.data < e)
            .group_by(Transaction.tipo)
            .all()
        )
        r, g = Decimal("0"), Decimal("0")
        for tipo, total in rows:
            if (tipo or "").upper() == "RECEITA":
                r += Decimal(total or 0)
            else:
                g += Decimal(total or 0)
        return r, g

    receitas, gastos = totals_for(start, end)
    receitas_to_date, gastos_to_date = totals_for(start, effective_end)

    prev_end = start
    prev_start = date(start.year - 1, 12, 1) if start.month == 1 else date(start.year, start.month - 1, 1)
    prev_receitas, prev_gastos = totals_for(prev_start, prev_end)

    def pct_change(cur: Decimal, prev: Decimal):
        if prev == 0:
            return None
        return float(((cur - prev) / prev) * 100)

    days_elapsed = max(1, (effective_end - start).days)
    days_in_month = (end - start).days
    projected_gastos = (gastos_to_date / Decimal(days_elapsed)) * Decimal(days_in_month)

    top_cats = (
        db.session.query(Transaction.categoria, func.coalesce(func.sum(Transaction.valor), 0))
        .filter(Transaction.user_id == uid, Transaction.tipo == "GASTO")
        .filter(Transaction.data >= start, Transaction.data < end)
        .group_by(Transaction.categoria)
        .order_by(func.sum(Transaction.valor).desc())
        .limit(3)
        .all()
    )

    daily = (
        db.session.query(Transaction.data, func.coalesce(func.sum(Transaction.valor), 0))
        .filter(Transaction.user_id == uid, Transaction.tipo == "GASTO")
        .filter(Transaction.data >= start, Transaction.data < end)
        .group_by(Transaction.data)
        .all()
    )
    daily_vals = [Decimal(v or 0) for _, v in daily]
    avg_daily = (sum(daily_vals) / Decimal(len(daily_vals))) if daily_vals else Decimal("0")
    spikes = []
    if avg_daily > 0:
        for d, v in daily:
            if Decimal(v or 0) >= (avg_daily * Decimal("2.5")):
                spikes.append({"data": d.isoformat(), "gasto": float(v)})

    return jsonify(
        mes=mes,
        ano=ano,
        totais={"receitas": float(receitas), "gastos": float(gastos), "saldo": float(receitas - gastos)},
        comparativo_mes_anterior={
            "receitas_pct": pct_change(receitas, prev_receitas),
            "gastos_pct": pct_change(gastos, prev_gastos),
        },
        projecao_gastos_mes=float(projected_gastos),
        top_categorias_gasto=[{"categoria": c, "total": float(v)} for c, v in top_cats],
        alertas={"picos_diarios": spikes[:10]},
    )


# -------------------------
# C) Recorrentes
# -------------------------
def _compute_next_run(frequency: str, ref: date, day_of_month: int | None, day_of_week: int | None) -> date:
    frequency = (frequency or "").upper()
    if frequency == "DAILY":
        return ref + timedelta(days=1)
    if frequency == "WEEKLY":
        dow = 0 if day_of_week is None else int(day_of_week)
        days_ahead = (dow - ref.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return ref + timedelta(days=days_ahead)

    dom = 1 if day_of_month is None else max(1, min(28, int(day_of_month)))
    y, m = ref.year, ref.month
    if m == 12:
        y, m = y + 1, 1
    else:
        m += 1
    return date(y, m, dom)


@app.get("/api/recorrentes")
def api_list_recorrentes():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    rules = (
        RecurringRule.query
        .filter_by(user_id=uid)
        .order_by(RecurringRule.active.desc(), RecurringRule.id.desc())
        .limit(100)
        .all()
    )
    items = []
    for r in rules:
        items.append({
            "id": r.id,
            "active": bool(r.active),
            "tipo": r.tipo,
            "categoria": r.categoria,
            "descricao": r.descricao or "",
            "valor": float(r.valor),
            "frequency": r.frequency,
            "day_of_month": r.day_of_month,
            "day_of_week": r.day_of_week,
            "start_date": r.start_date.isoformat() if r.start_date else None,
            "next_run": r.next_run.isoformat() if r.next_run else None,
        })
    return jsonify(items=items)


@app.post("/api/recorrentes")
def api_create_recorrente():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    dataj = request.get_json(silent=True) or {}

    tipo = str(dataj.get("tipo") or "").strip().upper()
    if tipo not in ("RECEITA", "GASTO"):
        return jsonify(error="Tipo inválido"), 400

    categoria = _safe_title(dataj.get("categoria") or "Outros")
    descricao = str(dataj.get("descricao") or "").strip() or None

    try:
        valor = _parse_brl_value(dataj.get("valor"))
    except Exception:
        return jsonify(error="Valor inválido"), 400

    frequency = str(dataj.get("frequency") or "MONTHLY").strip().upper()
    if frequency not in ("MONTHLY", "WEEKLY", "DAILY"):
        return jsonify(error="Frequência inválida (MONTHLY/WEEKLY/DAILY)"), 400

    start_date = _parse_date_any(dataj.get("start_date")) if dataj.get("start_date") else datetime.utcnow().date()
    day_of_month = dataj.get("day_of_month")
    day_of_week = dataj.get("day_of_week")

    if frequency == "MONTHLY":
        if day_of_month is None:
            day_of_month = start_date.day
        day_of_month = max(1, min(28, int(day_of_month)))
        day_of_week = None
        next_run = date(start_date.year, start_date.month, day_of_month)
        if next_run < start_date:
            next_run = _compute_next_run("MONTHLY", start_date, day_of_month, None)
    elif frequency == "WEEKLY":
        if day_of_week is None:
            day_of_week = start_date.weekday()
        day_of_week = max(0, min(6, int(day_of_week)))
        day_of_month = None
        next_run = start_date
        if next_run.weekday() != day_of_week:
            next_run = _compute_next_run("WEEKLY", start_date - timedelta(days=1), None, day_of_week)
    else:
        day_of_month = None
        day_of_week = None
        next_run = start_date

    r = RecurringRule(
        user_id=uid,
        active=True,
        tipo=tipo,
        categoria=categoria,
        descricao=descricao,
        valor=valor,
        frequency=frequency,
        day_of_month=day_of_month,
        day_of_week=day_of_week,
        start_date=start_date,
        next_run=next_run,
    )
    db.session.add(r)
    db.session.commit()
    return jsonify(ok=True, id=r.id)


@app.put("/api/recorrentes/<int:rid>")
def api_update_recorrente(rid: int):
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    r = RecurringRule.query.filter_by(id=rid, user_id=uid).first()
    if not r:
        return jsonify(error="Não encontrado"), 404

    dataj = request.get_json(silent=True) or {}

    if "active" in dataj:
        r.active = bool(dataj.get("active"))

    if "tipo" in dataj:
        tipo = str(dataj.get("tipo") or "").strip().upper()
        if tipo not in ("RECEITA", "GASTO"):
            return jsonify(error="Tipo inválido"), 400
        r.tipo = tipo

    if "categoria" in dataj:
        r.categoria = _safe_title(dataj.get("categoria") or r.categoria)

    if "descricao" in dataj:
        r.descricao = str(dataj.get("descricao") or "").strip() or None

    if "valor" in dataj:
        try:
            r.valor = _parse_brl_value(dataj.get("valor"))
        except Exception:
            return jsonify(error="Valor inválido"), 400

    db.session.commit()
    return jsonify(ok=True)


@app.delete("/api/recorrentes/<int:rid>")
def api_delete_recorrente(rid: int):
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    r = RecurringRule.query.filter_by(id=rid, user_id=uid).first()
    if not r:
        return jsonify(error="Não encontrado"), 404

    db.session.delete(r)
    db.session.commit()
    return jsonify(ok=True)


@app.post("/api/recorrentes/run")
def api_run_recorrentes():
    if not _panic_allowed():
        return jsonify({"error": "forbidden"}), 403

    today = datetime.utcnow().date()
    rules = (
        RecurringRule.query
        .filter(RecurringRule.active.is_(True))
        .filter(RecurringRule.next_run <= today)
        .order_by(RecurringRule.next_run.asc(), RecurringRule.id.asc())
        .limit(500)
        .all()
    )

    created = 0
    for r in rules:
        tx = Transaction(
            user_id=r.user_id,
            tipo=r.tipo,
            data=r.next_run,
            categoria=r.categoria,
            descricao=r.descricao,
            valor=r.valor,
            origem="APP",
        )
        db.session.add(tx)
        created += 1
        r.next_run = _compute_next_run(r.frequency, r.next_run, r.day_of_month, r.day_of_week)

    db.session.commit()
    return jsonify(ok=True, created=created)


# -------------------------
# D) Inteligência analítica
# -------------------------
@app.get("/api/analitico")
def api_analitico():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    try:
        mes = int(request.args.get("mes"))
        ano = int(request.args.get("ano"))
    except Exception:
        return jsonify(error="Parâmetros mes/ano inválidos"), 400

    start, end = _month_range(ano, mes)

    rows = (
        db.session.query(Transaction.tipo, func.coalesce(func.sum(Transaction.valor), 0))
        .filter(Transaction.user_id == uid)
        .filter(Transaction.data >= start, Transaction.data < end)
        .group_by(Transaction.tipo)
        .all()
    )
    receitas, gastos = Decimal("0"), Decimal("0")
    for tipo, total in rows:
        if (tipo or "").upper() == "RECEITA":
            receitas += Decimal(total or 0)
        else:
            gastos += Decimal(total or 0)

    saldo = receitas - gastos
    taxa = None
    if receitas > 0:
        taxa = float((saldo / receitas) * 100)

    top_cat = (
        db.session.query(Transaction.categoria, func.coalesce(func.sum(Transaction.valor), 0))
        .filter(Transaction.user_id == uid, Transaction.tipo == "GASTO")
        .filter(Transaction.data >= start, Transaction.data < end)
        .group_by(Transaction.categoria)
        .order_by(func.sum(Transaction.valor).desc())
        .first()
    )

    top_day = (
        db.session.query(Transaction.data, func.coalesce(func.sum(Transaction.valor), 0))
        .filter(Transaction.user_id == uid, Transaction.tipo == "GASTO")
        .filter(Transaction.data >= start, Transaction.data < end)
        .group_by(Transaction.data)
        .order_by(func.sum(Transaction.valor).desc())
        .first()
    )

    recomendacoes = []
    if receitas > 0 and gastos > receitas * Decimal("0.9"):
        recomendacoes.append("Seus gastos estão bem próximos da receita. Tente definir um teto semanal.")
    if top_cat and Decimal(top_cat[1] or 0) > (gastos * Decimal("0.35")) and gastos > 0:
        recomendacoes.append(f"Categoria '{top_cat[0]}' está puxando a maior parte do gasto. Vale revisar.")
    if taxa is not None and taxa < 5:
        recomendacoes.append("Taxa de poupança baixa (<5%). Pequenos cortes em categorias top ajudam.")

    if not recomendacoes:
        recomendacoes.append("Tudo ok 👍 Continue acompanhando e ajustando categorias/recorrentes.")

    return jsonify(
        mes=mes,
        ano=ano,
        totais={"receitas": float(receitas), "gastos": float(gastos), "saldo": float(saldo)},
        taxa_poupanca_pct=taxa,
        top_categoria_gasto={"categoria": top_cat[0], "total": float(top_cat[1])} if top_cat else None,
        dia_mais_caro={"data": top_day[0].isoformat(), "gasto": float(top_day[1])} if top_day else None,
        recomendacoes=recomendacoes[:5],
    )


# -------------------------
# WhatsApp - Inteligência (base + novos comandos)
# -------------------------
CONNECT_ALIASES = ("conectar", "vincular", "linkar", "associar", "registrar", "conexao", "conexão")
NEGATIONS = {"nao", "não", "nunca", "jamais"}

VALUE_RE = re.compile(r"([+\-])?\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:[\.,]\d{1,2})?)")

INCOME_HINTS = {
    "recebi", "recebido", "recebida", "entrou", "entrada", "caiu",
    "deposito", "depósito", "salario", "salário", "venda", "vendido",
    "comissao", "comissão", "bonus", "bônus", "reembolso", "ganhei", "renda", "receita",
    "pixrecebido", "pix_recebido",
}

EXPENSE_HINTS = {
    "paguei", "pago", "pagar", "comprei", "compra", "gastei", "gasto", "despesa",
    "saida", "saída", "debito", "débito", "boleto", "conta", "fatura", "cartao", "cartão",
}

DEFAULT_CATEGORY_KEYWORDS = [
    ("Alimentação", {"ifood", "i-food", "restaurante", "lanchonete", "pizza", "burguer", "hamburguer", "lanche", "mercado", "padaria", "cafe", "café"}),
    ("Transporte", {"uber", "99", "taxi", "táxi", "onibus", "ônibus", "metro", "metrô", "gasolina", "etanol", "combustivel", "combustível", "estacionamento"}),
    ("Moradia", {"aluguel", "condominio", "condomínio", "iptu", "prestacao", "prestação", "financiamento"}),
    ("Contas", {"luz", "energia", "agua", "água", "internet", "telefone", "celular", "netflix", "spotify", "assinatura", "streaming"}),
    ("Saúde", {"farmacia", "farmácia", "remedio", "remédio", "medico", "médico", "consulta", "exame", "dentista"}),
    ("Educação", {"curso", "faculdade", "escola", "mensalidade", "livro"}),
    ("Lazer", {"cinema", "show", "bar", "viagem", "hotel"}),
    ("Impostos", {"imposto", "taxa", "multa"}),
    ("Trabalho", {"salario", "salário", "pagamento", "prolabore", "pró-labore", "pro-labore", "freela", "freelancer"}),
    ("Transferências", {"pix", "ted", "doc", "transferencia", "transferência"}),
]


def _norm_word(w: str) -> str:
    w = (w or "").strip().lower()
    w = (
        w.replace("á", "a").replace("à", "a").replace("â", "a").replace("ã", "a")
         .replace("é", "e").replace("ê", "e")
         .replace("í", "i")
         .replace("ó", "o").replace("ô", "o").replace("õ", "o")
         .replace("ú", "u")
         .replace("ç", "c")
    )
    return w


def _tokenize(textv: str) -> list[str]:
    textv = _norm_word(textv)
    parts = re.split(r"[^a-z0-9]+", textv)
    return [p for p in parts if p]


def _detect_tipo_with_score(sign: str, before_tokens: list[str], after_tokens: list[str]):
    if sign == "+":
        return "RECEITA", "high"
    if sign == "-":
        return "GASTO", "high"

    bset = set(before_tokens)
    aset = set(after_tokens)

    income_set = {_norm_word(x) for x in INCOME_HINTS}
    expense_set = {_norm_word(x) for x in EXPENSE_HINTS}

    b_income = len(bset & income_set)
    b_exp = len(bset & expense_set)
    a_income = len(aset & income_set)
    a_exp = len(aset & expense_set)

    score_income = (b_income * 3) + a_income
    score_exp = (b_exp * 3) + a_exp

    has_neg = any(t in {_norm_word(n) for n in NEGATIONS} for t in before_tokens[:2])
    if has_neg and score_income > 0 and score_exp == 0:
        score_income = 0

    if score_income == 0 and score_exp == 0:
        return "GASTO", "low"

    if score_income == score_exp:
        return ("RECEITA" if score_income > 0 else "GASTO"), "low"

    if score_income > score_exp:
        return "RECEITA", ("high" if (score_income - score_exp) >= 2 else "low")
    return "GASTO", ("high" if (score_exp - score_income) >= 2 else "low")


def _guess_category_from_text(user_id: int, full_text: str) -> str | None:
    tokens = set(_tokenize(full_text))

    try:
        rules = (
            CategoryRule.query
            .filter(CategoryRule.user_id == user_id)
            .order_by(CategoryRule.priority.desc(), CategoryRule.id.desc())
            .all()
        )
        for r in rules:
            key = _norm_word(r.pattern)
            if not key:
                continue
            if key in tokens or any(key in t for t in tokens):
                return (r.categoria or "").strip().title() or None
    except Exception:
        pass

    for cat, keys in DEFAULT_CATEGORY_KEYWORDS:
        nkeys = {_norm_word(k) for k in keys}
        if tokens & nkeys:
            return cat

    return None


CMD_HELP_RE = re.compile(r"^\s*(ajuda|\?|help)\s*$", re.IGNORECASE)
CMD_ULTIMOS_RE = re.compile(r"^\s*ultimos\s*$", re.IGNORECASE)
CMD_APAGAR_RE = re.compile(r"^\s*apagar\s+(\d+)\s*$", re.IGNORECASE)
CMD_CORRIGIR_ULTIMA_RE = re.compile(r"^\s*corrigir\s+ultima\s+(.+)$", re.IGNORECASE)
CMD_EDITAR_RE = re.compile(r"^\s*editar\s+(\d+)\s+(.+)$", re.IGNORECASE)

CMD_RESUMO_RE = re.compile(r"^\s*resumo(\s+(\d{1,2})[\/\-](\d{4}))?\s*$", re.IGNORECASE)
CMD_ANALISE_RE = re.compile(r"^\s*(analise|análise|insights)\s*(\d{1,2}[\/\-]\d{4})?\s*$", re.IGNORECASE)
CMD_RECORRENTES_RE = re.compile(r"^\s*recorrentes\s*$", re.IGNORECASE)
CMD_RODAR_RECORRENTES_RE = re.compile(r"^\s*rodar\s+recorrentes\s*$", re.IGNORECASE)

CAT_SET_RE = re.compile(r"^\s*categoria\s+(.+?)\s*=\s*(.+?)\s*$", re.IGNORECASE)
CAT_DEL_RE = re.compile(r"^\s*remover\s+categoria\s+(.+?)\s*$", re.IGNORECASE)
CAT_LIST_RE = re.compile(r"^\s*categorias\s*$", re.IGNORECASE)


def _wa_help_text():
    return (
        "✅ Comandos disponíveis:\n\n"
        "🔗 Conectar:\n"
        "• conectar seuemail@dominio.com\n\n"
        "🧾 Lançar:\n"
        "• recebi 1200 salario\n"
        "• paguei 32,90 mercado\n"
        "• + 35,90 venda camiseta\n"
        "• - 18,00 uber\n\n"
        "🧠 Quando houver dúvida, eu pergunto: RECEITA ou GASTO.\n"
        "Aí você responde apenas: receita  (ou)  gasto\n\n"
        "✏️ Corrigir direto aqui:\n"
        "• ultimos\n"
        "• apagar 123\n"
        "• editar 123 valor=35,90 categoria=Alimentação data=2026-03-01 descricao=algo tipo=receita\n"
        "• corrigir ultima categoria=Transporte\n\n"
        "📊 Resumos/Análises:\n"
        "• resumo\n"
        "• resumo 03/2026\n"
        "• analise\n"
        "• analise 03/2026\n\n"
        "🔁 Recorrentes:\n"
        "• recorrentes\n"
        "• rodar recorrentes\n\n"
        "🏷️ Ensinar categorias:\n"
        "• categorias\n"
        "• categoria ifood = Alimentação\n"
        "• remover categoria ifood\n"
    )


def _parse_kv_assignments(s: str) -> dict:
    out = {}
    pattern = re.compile(r'(\w+)\s*=\s*(".*?"|\'.*?\'|[^\s]+)')
    for m in pattern.finditer(s):
        k = m.group(1).strip().lower()
        v = m.group(2).strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        out[k] = v
    return out


def _pending_get(wa_from: str):
    now = datetime.utcnow()
    WaPending.query.filter(WaPending.wa_from == wa_from, WaPending.expires_at < now).delete()
    db.session.commit()
    return (
        WaPending.query
        .filter(WaPending.wa_from == wa_from, WaPending.expires_at >= now)
        .order_by(WaPending.id.desc())
        .first()
    )


def _pending_set(wa_from: str, user_id: int, kind: str, payload: dict, minutes: int = 10):
    WaPending.query.filter_by(wa_from=wa_from, user_id=user_id).delete()
    db.session.commit()
    p = WaPending(
        wa_from=wa_from,
        user_id=user_id,
        kind=kind,
        payload_json=json.dumps(payload, ensure_ascii=False),
        expires_at=datetime.utcnow() + timedelta(minutes=minutes),
    )
    db.session.add(p)
    db.session.commit()


def _pending_clear(wa_from: str, user_id: int):
    WaPending.query.filter_by(wa_from=wa_from, user_id=user_id).delete()
    db.session.commit()


def _parse_wa_text(msg_text: str):
    t = (msg_text or "").strip()
    if not t:
        return {"cmd": "NONE"}

    if CMD_HELP_RE.match(t):
        return {"cmd": "HELP"}

    m = CMD_RESUMO_RE.match(t)
    if m:
        mm = m.group(2)
        yy = m.group(3)
        return {"cmd": "RESUMO", "mes": int(mm) if mm else None, "ano": int(yy) if yy else None}

    m = CMD_ANALISE_RE.match(t)
    if m:
        p = m.group(2)
        if p and re.match(r"^\d{1,2}[\/\-]\d{4}$", p.strip()):
            mm, yy = re.split(r"[\/\-]", p.strip())
            return {"cmd": "ANALISE", "mes": int(mm), "ano": int(yy)}
        return {"cmd": "ANALISE", "mes": None, "ano": None}

    if CMD_RECORRENTES_RE.match(t):
        return {"cmd": "RECORRENTES"}
    if CMD_RODAR_RECORRENTES_RE.match(t):
        return {"cmd": "RODAR_RECORRENTES"}

    mset = CAT_SET_RE.match(t)
    if mset:
        key = mset.group(1).strip()
        cat = mset.group(2).strip()
        if not key or not cat:
            return {"cmd": "CAT_HELP"}
        return {"cmd": "CAT_SET", "key": key, "categoria": cat}

    mdel = CAT_DEL_RE.match(t)
    if mdel:
        key = mdel.group(1).strip()
        if not key:
            return {"cmd": "CAT_HELP"}
        return {"cmd": "CAT_DEL", "key": key}

    if CAT_LIST_RE.match(t):
        return {"cmd": "CAT_LIST"}

    if CMD_ULTIMOS_RE.match(t):
        return {"cmd": "ULTIMOS"}

    m = CMD_APAGAR_RE.match(t)
    if m:
        return {"cmd": "APAGAR", "id": int(m.group(1))}

    m = CMD_CORRIGIR_ULTIMA_RE.match(t)
    if m:
        return {"cmd": "CORRIGIR_ULTIMA", "fields": _parse_kv_assignments(m.group(1))}

    m = CMD_EDITAR_RE.match(t)
    if m:
        return {"cmd": "EDITAR", "id": int(m.group(1)), "fields": _parse_kv_assignments(m.group(2))}

    low_simple = _norm_word(t)
    if low_simple in ("receita", "gasto"):
        return {"cmd": "CONFIRM_TIPO", "tipo": "RECEITA" if low_simple == "receita" else "GASTO"}

    low = _norm_word(t)
    low = re.sub(r"\s+", " ", low).strip()

    for alias in CONNECT_ALIASES:
        if low.startswith(_norm_word(alias) + " "):
            email = t.split(" ", 1)[1].strip()
            return {"cmd": "CONNECT", "email": _normalize_email(email)}

    m = VALUE_RE.search(low)
    if not m:
        return {"cmd": "NONE"}

    sign = m.group(1) or ""
    valor_raw = m.group(2)
    try:
        valor = _parse_brl_value(valor_raw)
    except Exception:
        return {"cmd": "NONE"}

    before = (low[: m.start()] or "").strip()
    after = (low[m.end() :] or "").strip(" -–—")

    before_tokens = _tokenize(before)
    after_tokens = _tokenize(after)

    tipo, confidence = _detect_tipo_with_score(sign, before_tokens, after_tokens)

    categoria_fallback = "Outros"
    descricao = ""
    if after:
        parts = after.split(" ", 1)
        categoria_fallback = (parts[0] or "Outros").strip().title()
        descricao = parts[1].strip() if len(parts) > 1 else ""

    return {
        "cmd": "TX",
        "tipo": tipo,
        "tipo_confidence": confidence,
        "valor": valor,
        "categoria_fallback": categoria_fallback,
        "descricao": descricao,
        "data": datetime.utcnow().date(),
        "raw_text": t,
    }


def _apply_edit_fields(tx: Transaction, fields: dict) -> tuple[bool, str]:
    if not fields:
        return False, "Nenhum campo informado."

    if "tipo" in fields:
        v = _norm_word(fields["tipo"])
        if v in ("receita", "gasto"):
            tx.tipo = "RECEITA" if v == "receita" else "GASTO"
        else:
            return False, "Tipo inválido. Use tipo=receita ou tipo=gasto"

    if "valor" in fields:
        try:
            tx.valor = _parse_brl_value(fields["valor"])
        except Exception:
            return False, "Valor inválido. Ex: valor=35,90"

    if "data" in fields:
        try:
            tx.data = _parse_date_any(fields["data"])
        except Exception:
            return False, "Data inválida. Ex: data=2026-03-01"

    if "categoria" in fields:
        cat = str(fields["categoria"] or "").strip()
        if not cat:
            return False, "Categoria vazia."
        tx.categoria = cat.title()

    if "descricao" in fields:
        desc = str(fields["descricao"] or "").strip()
        tx.descricao = desc or None

    return True, "OK"


def _wa_month_from_payload(mes: int | None, ano: int | None):
    today = datetime.utcnow().date()
    if mes is None or ano is None:
        return today.month, today.year
    return max(1, min(12, int(mes))), int(ano)


# -------------------------
# WhatsApp Cloud API Webhook
# -------------------------
@app.get("/webhooks/whatsapp")
def wa_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        return challenge or "", 200
    return "forbidden", 403


@app.post("/webhooks/whatsapp")
def wa_webhook():
    payload = request.get_json(silent=True) or {}

    try:
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                for msg in value.get("messages", []) or []:
                    if msg.get("type") != "text":
                        continue

                    msg_id = msg.get("id")
                    wa_from = _normalize_wa_number(msg.get("from") or "")
                    body = ((msg.get("text") or {}) or {}).get("body", "") or ""

                    if msg_id and ProcessedMessage.query.filter_by(msg_id=msg_id).first():
                        continue
                    if msg_id:
                        db.session.add(ProcessedMessage(msg_id=msg_id, wa_from=wa_from))
                        db.session.commit()

                    parsed = _parse_wa_text(body)

                    if parsed["cmd"] == "HELP":
                        wa_send_text(wa_from, _wa_help_text())
                        continue

                    if parsed["cmd"] == "CONNECT":
                        email = parsed.get("email")
                        if not email or "@" not in email:
                            wa_send_text(wa_from, "Email inválido. Ex: conectar david@email.com")
                            continue

                        u = _get_or_create_user_by_email(email, password=None)

                        link = WaLink.query.filter_by(wa_from=wa_from).first()
                        already = False
                        if link:
                            already = (link.user_id == u.id)
                            link.user_id = u.id
                        else:
                            link = WaLink(wa_from=wa_from, user_id=u.id)
                            db.session.add(link)
                        db.session.commit()

                        wa_send_text(
                            wa_from,
                            (f"✅ {'Já estava' if already else 'WhatsApp'} conectado ao email: {email}\n\n"
                             "Digite 'ajuda' para ver todos os comandos.\n"
                             "Exemplo: paguei 32,90 mercado"),
                        )
                        continue

                    link = WaLink.query.filter_by(wa_from=wa_from).first()
                    if not link:
                        wa_send_text(
                            wa_from,
                            "🔒 Seu WhatsApp não está conectado.\n\nEnvie:\n"
                            "conectar SEU_EMAIL_DO_APP\n"
                            "Ex: conectar david@email.com\n\n"
                            "Depois digite: ajuda",
                        )
                        continue

                    if parsed["cmd"] == "RESUMO":
                        mes, ano = _wa_month_from_payload(parsed.get("mes"), parsed.get("ano"))
                        start, end = _month_range(ano, mes)

                        rows = (
                            db.session.query(Transaction.tipo, func.coalesce(func.sum(Transaction.valor), 0))
                            .filter(Transaction.user_id == link.user_id)
                            .filter(Transaction.data >= start, Transaction.data < end)
                            .group_by(Transaction.tipo)
                            .all()
                        )
                        receitas, gastos = Decimal("0"), Decimal("0")
                        for tipo, total in rows:
                            if (tipo or "").upper() == "RECEITA":
                                receitas += Decimal(total or 0)
                            else:
                                gastos += Decimal(total or 0)
                        saldo = receitas - gastos

                        top_cats = (
                            db.session.query(Transaction.categoria, func.coalesce(func.sum(Transaction.valor), 0))
                            .filter(Transaction.user_id == link.user_id, Transaction.tipo == "GASTO")
                            .filter(Transaction.data >= start, Transaction.data < end)
                            .group_by(Transaction.categoria)
                            .order_by(func.sum(Transaction.valor).desc())
                            .limit(3)
                            .all()
                        )

                        lines = [
                            f"📊 Resumo {mes:02d}/{ano}",
                            f"Receitas: R$ {_format_brl(receitas)}",
                            f"Gastos:   R$ {_format_brl(gastos)}",
                            f"Saldo:    R$ {_format_brl(saldo)}",
                        ]
                        if top_cats:
                            lines.append("\nTop gastos por categoria:")
                            for c, v in top_cats:
                                lines.append(f"• {c}: R$ {_format_brl(Decimal(v or 0))}")
                        else:
                            lines.append("\nSem gastos no período.")

                        wa_send_text(wa_from, "\n".join(lines))
                        continue

                    if parsed["cmd"] == "ANALISE":
                        mes, ano = _wa_month_from_payload(parsed.get("mes"), parsed.get("ano"))
                        start, end = _month_range(ano, mes)

                        rows = (
                            db.session.query(Transaction.tipo, func.coalesce(func.sum(Transaction.valor), 0))
                            .filter(Transaction.user_id == link.user_id)
                            .filter(Transaction.data >= start, Transaction.data < end)
                            .group_by(Transaction.tipo)
                            .all()
                        )
                        receitas, gastos = Decimal("0"), Decimal("0")
                        for tipo, total in rows:
                            if (tipo or "").upper() == "RECEITA":
                                receitas += Decimal(total or 0)
                            else:
                                gastos += Decimal(total or 0)
                        saldo = receitas - gastos

                        prev_end = start
                        prev_start = date(start.year - 1, 12, 1) if start.month == 1 else date(start.year, start.month - 1, 1)
                        prev_rows = (
                            db.session.query(Transaction.tipo, func.coalesce(func.sum(Transaction.valor), 0))
                            .filter(Transaction.user_id == link.user_id)
                            .filter(Transaction.data >= prev_start, Transaction.data < prev_end)
                            .group_by(Transaction.tipo)
                            .all()
                        )
                        prev_receitas, prev_gastos = Decimal("0"), Decimal("0")
                        for tipo, total in prev_rows:
                            if (tipo or "").upper() == "RECEITA":
                                prev_receitas += Decimal(total or 0)
                            else:
                                prev_gastos += Decimal(total or 0)

                        def pct(cur: Decimal, prev: Decimal):
                            if prev == 0:
                                return None
                            return float(((cur - prev) / prev) * 100)

                        recs = []
                        if receitas > 0 and gastos > receitas * Decimal("0.9"):
                            recs.append("Gastos muito próximos da receita. Defina um teto semanal.")
                        if receitas > 0:
                            taxa = (saldo / receitas) * Decimal("100")
                            if taxa < 5:
                                recs.append("Taxa de poupança baixa (<5%). Pequenos cortes ajudam.")
                            else:
                                recs.append(f"Taxa de poupança: {float(taxa):.1f}% 👍")
                        else:
                            recs.append("Sem receitas no mês — se for um mês parcial, tudo bem.")

                        lines = [
                            f"🧠 Análise {mes:02d}/{ano}",
                            f"Receitas: R$ {_format_brl(receitas)}",
                            f"Gastos:   R$ {_format_brl(gastos)}",
                            f"Saldo:    R$ {_format_brl(saldo)}",
                        ]
                        gchg = pct(gastos, prev_gastos)
                        if gchg is not None:
                            lines.append(f"Variação de gastos vs mês anterior: {gchg:+.1f}%")
                        rchg = pct(receitas, prev_receitas)
                        if rchg is not None:
                            lines.append(f"Variação de receitas vs mês anterior: {rchg:+.1f}%")
                        lines.append("\nDicas:")
                        for r in recs[:3]:
                            lines.append(f"• {r}")

                        wa_send_text(wa_from, "\n".join(lines))
                        continue

                    if parsed["cmd"] == "RECORRENTES":
                        rules = (
                            RecurringRule.query
                            .filter_by(user_id=link.user_id)
                            .order_by(RecurringRule.active.desc(), RecurringRule.id.desc())
                            .limit(10)
                            .all()
                        )
                        if not rules:
                            wa_send_text(
                                wa_from,
                                "Você ainda não tem recorrentes.\n\n"
                                "Crie pelo app (API /api/recorrentes).\n"
                                "Depois você pode digitar: rodar recorrentes",
                            )
                        else:
                            lines = ["🔁 Recorrentes (top 10):"]
                            for r in rules:
                                freq = r.frequency
                                if freq == "MONTHLY":
                                    when = f"dia {r.day_of_month or 1}"
                                elif freq == "WEEKLY":
                                    when = f"dow {r.day_of_week}"
                                else:
                                    when = "diário"
                                lines.append(
                                    f"• ID {r.id} | {'ON' if r.active else 'OFF'} | {r.tipo} | R$ {_format_brl(r.valor)} | {r.categoria} | {freq} {when} | next {r.next_run.isoformat()}"
                                )
                            wa_send_text(wa_from, "\n".join(lines))
                        continue

                    if parsed["cmd"] == "RODAR_RECORRENTES":
                        today = datetime.utcnow().date()
                        rules = (
                            RecurringRule.query
                            .filter_by(user_id=link.user_id)
                            .filter(RecurringRule.active.is_(True))
                            .filter(RecurringRule.next_run <= today)
                            .order_by(RecurringRule.next_run.asc(), RecurringRule.id.asc())
                            .limit(100)
                            .all()
                        )
                        if not rules:
                            wa_send_text(wa_from, "Nada para rodar agora. (Nenhuma recorrência vencida)")
                            continue

                        created = 0
                        for r in rules:
                            tx = Transaction(
                                user_id=r.user_id,
                                tipo=r.tipo,
                                data=r.next_run,
                                categoria=r.categoria,
                                descricao=r.descricao,
                                valor=r.valor,
                                origem="APP",
                            )
                            db.session.add(tx)
                            created += 1
                            r.next_run = _compute_next_run(r.frequency, r.next_run, r.day_of_month, r.day_of_week)
                        db.session.commit()
                        wa_send_text(wa_from, f"✅ Recorrentes processados: {created} lançamento(s) criados.")
                        continue

                    if parsed["cmd"] == "CONFIRM_TIPO":
                        pending = _pending_get(wa_from)
                        if not pending or pending.user_id != link.user_id:
                            wa_send_text(wa_from, "Não tenho nenhuma dúvida pendente agora. Digite 'ajuda'.")
                            continue
                        if pending.kind != "CONFIRM_TIPO":
                            wa_send_text(wa_from, "Pendência não reconhecida. Digite 'ajuda'.")
                            continue

                        payload_tx = json.loads(pending.payload_json)
                        payload_tx["tipo"] = parsed["tipo"]

                        guessed = _guess_category_from_text(link.user_id, payload_tx.get("raw_text", ""))
                        categoria = guessed or payload_tx.get("categoria_fallback") or "Outros"

                        ttx = Transaction(
                            user_id=link.user_id,
                            tipo=payload_tx["tipo"],
                            data=_parse_date_any(payload_tx.get("data")),
                            categoria=categoria,
                            descricao=(payload_tx.get("descricao") or None),
                            valor=_parse_brl_value(payload_tx.get("valor")),
                            origem="WA",
                        )
                        db.session.add(ttx)
                        db.session.commit()
                        _pending_clear(wa_from, link.user_id)

                        wa_send_text(
                            wa_from,
                            "✅ Lançamento salvo (confirmado)!\n"
                            f"ID: {ttx.id}\n"
                            f"Tipo: {ttx.tipo}\n"
                            f"Valor: R$ {_format_brl(ttx.valor)}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}",
                        )
                        continue

                    if parsed["cmd"] == "CAT_HELP":
                        wa_send_text(
                            wa_from,
                            "Use assim:\n"
                            "• categoria ifood = Alimentação\n"
                            "• remover categoria ifood\n"
                            "• categorias",
                        )
                        continue

                    if parsed["cmd"] == "CAT_SET":
                        key = (parsed.get("key") or "").strip()
                        cat = (parsed.get("categoria") or "").strip()
                        if not key or not cat:
                            wa_send_text(wa_from, "Formato inválido. Ex: categoria ifood = Alimentação")
                            continue

                        key_norm = _norm_word(key)
                        if len(key_norm) < 2:
                            wa_send_text(wa_from, "Chave muito curta. Ex: categoria uber = Transporte")
                            continue

                        existing = CategoryRule.query.filter_by(user_id=link.user_id, pattern=key_norm).first()
                        if existing:
                            existing.categoria = cat.title()
                            existing.priority = 10
                        else:
                            db.session.add(
                                CategoryRule(
                                    user_id=link.user_id,
                                    pattern=key_norm,
                                    categoria=cat.title(),
                                    priority=10,
                                )
                            )
                        db.session.commit()

                        wa_send_text(wa_from, f"✅ Regra salva: '{key_norm}' => {cat.title()}")
                        continue

                    if parsed["cmd"] == "CAT_DEL":
                        key = _norm_word(parsed.get("key") or "")
                        if not key:
                            wa_send_text(wa_from, "Formato inválido. Ex: remover categoria uber")
                            continue
                        q = CategoryRule.query.filter_by(user_id=link.user_id, pattern=key)
                        deleted = q.delete()
                        db.session.commit()
                        wa_send_text(wa_from, "✅ Regra removida." if deleted else "ℹ️ Essa regra não existia.")
                        continue

                    if parsed["cmd"] == "CAT_LIST":
                        rules = (
                            CategoryRule.query
                            .filter_by(user_id=link.user_id)
                            .order_by(CategoryRule.priority.desc(), CategoryRule.id.desc())
                            .limit(30)
                            .all()
                        )
                        if not rules:
                            wa_send_text(
                                wa_from,
                                "Você ainda não criou regras.\n\n"
                                "Exemplos:\n"
                                "• categoria ifood = Alimentação\n"
                                "• categoria uber = Transporte\n\n"
                                "Dica: o bot também tem categorias automáticas padrão.",
                            )
                        else:
                            lines = ["✅ Suas regras (até 30):"]
                            for r in rules:
                                lines.append(f"• {r.pattern} => {r.categoria}")
                            wa_send_text(wa_from, "\n".join(lines))
                        continue

                    if parsed["cmd"] == "ULTIMOS":
                        txs = (
                            Transaction.query
                            .filter(Transaction.user_id == link.user_id)
                            .order_by(Transaction.id.desc())
                            .limit(5)
                            .all()
                        )
                        if not txs:
                            wa_send_text(wa_from, "Você ainda não tem lançamentos.")
                        else:
                            lines = ["🧾 Últimos 5 lançamentos:"]
                            for ttx in txs:
                                lines.append(
                                    f"• ID {ttx.id} | {ttx.tipo} | R$ {_format_brl(ttx.valor)} | {ttx.categoria} | {ttx.data.isoformat()}"
                                )
                            lines.append("\nPara editar: editar ID valor=... categoria=... data=... tipo=receita/gasto")
                            lines.append("Para apagar: apagar ID")
                            wa_send_text(wa_from, "\n".join(lines))
                        continue

                    if parsed["cmd"] == "APAGAR":
                        txid = parsed["id"]
                        ttx = Transaction.query.filter_by(id=txid, user_id=link.user_id).first()
                        if not ttx:
                            wa_send_text(wa_from, "Não achei esse ID (ou não é seu). Use: ultimos")
                            continue
                        db.session.delete(ttx)
                        db.session.commit()
                        wa_send_text(wa_from, f"✅ Apagado: ID {txid}")
                        continue

                    if parsed["cmd"] == "EDITAR":
                        txid = parsed["id"]
                        fields = parsed.get("fields") or {}
                        ttx = Transaction.query.filter_by(id=txid, user_id=link.user_id).first()
                        if not ttx:
                            wa_send_text(wa_from, "Não achei esse ID (ou não é seu). Use: ultimos")
                            continue

                        ok, msg = _apply_edit_fields(ttx, fields)
                        if not ok:
                            wa_send_text(wa_from, f"❌ Não consegui editar: {msg}")
                            continue

                        db.session.commit()
                        wa_send_text(
                            wa_from,
                            "✅ Editado!\n"
                            f"ID: {ttx.id}\n"
                            f"Tipo: {ttx.tipo}\n"
                            f"Valor: R$ {_format_brl(ttx.valor)}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}",
                        )
                        continue

                    if parsed["cmd"] == "CORRIGIR_ULTIMA":
                        fields = parsed.get("fields") or {}
                        ttx = (
                            Transaction.query
                            .filter(Transaction.user_id == link.user_id)
                            .order_by(Transaction.id.desc())
                            .first()
                        )
                        if not ttx:
                            wa_send_text(wa_from, "Você ainda não tem lançamentos.")
                            continue

                        ok, msg = _apply_edit_fields(ttx, fields)
                        if not ok:
                            wa_send_text(wa_from, f"❌ Não consegui corrigir: {msg}")
                            continue

                        db.session.commit()
                        wa_send_text(
                            wa_from,
                            "✅ Corrigido na última transação!\n"
                            f"ID: {ttx.id}\n"
                            f"Tipo: {ttx.tipo}\n"
                            f"Valor: R$ {_format_brl(ttx.valor)}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}",
                        )
                        continue

                    if parsed["cmd"] == "TX":
                        raw_text = parsed.get("raw_text") or ""
                        guessed = _guess_category_from_text(link.user_id, raw_text)
                        categoria = guessed or parsed.get("categoria_fallback") or "Outros"

                        if parsed.get("tipo_confidence") == "low":
                            _pending_set(
                                wa_from=wa_from,
                                user_id=link.user_id,
                                kind="CONFIRM_TIPO",
                                payload={
                                    "tipo": parsed["tipo"],
                                    "valor": str(parsed["valor"]),
                                    "categoria_fallback": parsed.get("categoria_fallback"),
                                    "descricao": parsed.get("descricao") or "",
                                    "data": parsed.get("data").isoformat() if isinstance(parsed.get("data"), date) else None,
                                    "raw_text": raw_text,
                                },
                                minutes=10,
                            )
                            wa_send_text(
                                wa_from,
                                "🤔 Fiquei em dúvida se isso foi *RECEITA* ou *GASTO*.\n\n"
                                f"Mensagem: {raw_text}\n"
                                f"Valor: R$ {_format_brl(parsed['valor'])}\n"
                                f"Categoria sugerida: {categoria}\n\n"
                                "Responda apenas com:\n"
                                "• receita\n"
                                "ou\n"
                                "• gasto",
                            )
                            continue

                        ttx = Transaction(
                            user_id=link.user_id,
                            tipo=parsed["tipo"],
                            data=parsed["data"],
                            categoria=categoria,
                            descricao=(parsed.get("descricao") or None),
                            valor=parsed["valor"],
                            origem="WA",
                        )
                        db.session.add(ttx)
                        db.session.commit()

                        wa_send_text(
                            wa_from,
                            "✅ Lançamento salvo!\n"
                            f"ID: {ttx.id}\n"
                            f"Tipo: {ttx.tipo}\n"
                            f"Valor: R$ {_format_brl(ttx.valor)}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}\n\n"
                            "Dica: digite 'ultimos' para ver e editar.",
                        )
                        continue

                    wa_send_text(wa_from, "Não entendi. Digite: ajuda")

    except Exception as e:
        print("WA webhook error:", repr(e))

    return "ok", 200


# -------------------------
# Entry
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
