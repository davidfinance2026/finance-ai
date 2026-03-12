# -*- coding: utf-8 -*-
import os
import re
import json
import hashlib
import calendar
import base64
import tempfile
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask, request, jsonify, send_from_directory, session, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, inspect
from sqlalchemy.exc import SQLAlchemyError
from urllib.parse import quote
from PyPDF2 import PdfReader

from utils_core import (
    hash_password,
    normalize_email,
    parse_brl_value,
    parse_date_any,
    parse_money_br_to_decimal,
    iso_date,
    extract_json_from_text,
    fmt_brl,
    month_bounds,
    norm_word,
    tokenize,
    normalize_wa_number,
    period_range,
    next_monthly_date,
    next_weekly_date,
)
from utils_auth import (
    get_logged_user_id,
    get_logged_email,
    require_login,
    get_or_create_user_by_email,
    login_user,
    status_payload,
)

from utils_integrations import (
    init_integrations,
    wa_send_text,
    _openai_available,
    _openai_headers,
    _download_whatsapp_media,
    _transcribe_audio_file,
    _extract_pdf_text,
    _normalize_ai_result,
    _call_openai_finance_json,
    _analyze_text_transaction,
    _analyze_image_transaction,
)

from utils_workflows import (
    _apply_edit_fields,
    _create_recurring_rule,
    _handle_pending_ai_confirmation,
    _handle_whatsapp_media_message,
    _parse_kv_assignments,
    _parse_recorrente_args,
    _pending_clear,
    _pending_get,
    _pending_set,
    _run_recorrentes_for_user,
)

from finance_services import (
    init_finance_services,
    guess_category_from_text,
    sum_period,
    calc_projection,
    calc_alerts,
    calc_patrimonio_series,
    sum_investments_position,
    build_ai_finance_context,
    looks_like_finance_question,
    ask_openai_finance_assistant,
    reply_finance_question,
    make_resumo_text,
    make_analise_text,
    make_projection_text,
    make_alerts_text,
)

from whatsapp_commands import (
    parse_wa_text,
    wa_help_text,
)

from routes.auth_routes import register_auth_routes
from routes.account_routes import register_account_routes



# -------------------------
# App / Config
# -------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
app.config["JSON_AS_ASCII"] = False

# Cookies de sessão
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

# Senha mínima
MIN_PASSWORD_LEN = int(os.getenv("MIN_PASSWORD_LEN", "6"))

# WhatsApp Cloud API
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "").strip()
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "").strip()
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "").strip()
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v20.0").strip()

# Botão de pânico
PANIC_TOKEN = os.getenv("PANIC_TOKEN", "").strip()

# WhatsApp público
WA_PUBLIC_NUMBER = os.getenv("WA_PUBLIC_NUMBER", "5537998675231").strip()

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1-mini").strip()
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", OPENAI_CHAT_MODEL).strip()
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe").strip()

init_integrations(
    wa_access_token=WA_ACCESS_TOKEN,
    wa_phone_number_id=WA_PHONE_NUMBER_ID,
    graph_version=GRAPH_VERSION,
    openai_api_key=OPENAI_API_KEY,
    openai_chat_model=OPENAI_CHAT_MODEL,
    openai_vision_model=OPENAI_VISION_MODEL,
    openai_transcribe_model=OPENAI_TRANSCRIBE_MODEL,
)

# -------------------------
# DB
# -------------------------
db = SQLAlchemy(app)


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=True)
    password_hash = db.Column(db.String(64), nullable=False)
    password_set = db.Column(db.Boolean, nullable=False, server_default=text("false"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Transaction(db.Model):
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    tipo = db.Column(db.String(16), nullable=False)  # RECEITA/GASTO
    data = db.Column(db.Date, nullable=False, index=True)
    categoria = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    valor = db.Column(db.Numeric(12, 2), nullable=False)
    origem = db.Column(db.String(16), nullable=False, default="APP")  # APP/WA/REC
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Investment(db.Model):
    __tablename__ = "investments"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    data = db.Column(db.Date, nullable=False)
    ativo = db.Column(db.String(120), nullable=False)
    tipo = db.Column(db.String(20), nullable=False, default="APORTE")  # APORTE | RESGATE
    valor = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    descricao = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("investments", lazy=True))


class WaLink(db.Model):
    __tablename__ = "wa_links"
    id = db.Column(db.Integer, primary_key=True)
    wa_from = db.Column(db.String(40), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ProcessedMessage(db.Model):
    __tablename__ = "processed_messages"
    id = db.Column(db.Integer, primary_key=True)
    msg_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    wa_from = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class CategoryRule(db.Model):
    __tablename__ = "category_rules"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    pattern = db.Column(db.String(80), nullable=False)
    categoria = db.Column(db.String(80), nullable=False)
    priority = db.Column(db.Integer, nullable=False, server_default=text("10"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class WaPending(db.Model):
    __tablename__ = "wa_pending"
    id = db.Column(db.Integer, primary_key=True)
    wa_from = db.Column(db.String(40), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    kind = db.Column(db.String(40), nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class RecurringRule(db.Model):
    __tablename__ = "recurring_rules"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    freq = db.Column(db.String(16), nullable=False)  # DAILY/WEEKLY/MONTHLY
    day_of_month = db.Column(db.Integer, nullable=True)
    weekday = db.Column(db.Integer, nullable=True)  # 0=seg ... 6=dom

    tipo = db.Column(db.String(16), nullable=False)  # RECEITA/GASTO
    valor = db.Column(db.Numeric(12, 2), nullable=False)
    categoria = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.Text, nullable=True)

    start_date = db.Column(db.Date, nullable=False, default=lambda: datetime.utcnow().date())
    next_run = db.Column(db.Date, nullable=False, index=True)
    is_active = db.Column(db.Boolean, nullable=False, server_default=text("true"))

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


init_finance_services(
    Transaction=Transaction,
    Investment=Investment,
    RecurringRule=RecurringRule,
    CategoryRule=CategoryRule,
    openai_chat_model=OPENAI_CHAT_MODEL,
    openai_available_func=_openai_available,
    openai_headers_func=_openai_headers,
)


def _create_tables_if_needed():
    try:
        db.create_all()
    except Exception as e:
        print("DB create_all failed:", repr(e))


def _bootstrap_schema():
    """Migração leve/idempotente."""
    try:
        insp = inspect(db.engine)
        dialect = db.engine.dialect.name

        def has_table(t: str) -> bool:
            try:
                return insp.has_table(t)
            except Exception:
                return t in insp.get_table_names()

        def has_col(t: str, c: str) -> bool:
            if not has_table(t):
                return False
            cols = {col.get("name") for col in insp.get_columns(t)}
            return c in cols

        def add_col(t: str, col_name: str, col_ddl: str):
            if dialect == "postgresql":
                db.session.execute(text(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {col_name} {col_ddl}"))
                db.session.commit()
                return

            if has_col(t, col_name):
                return

            try:
                db.session.execute(text(f"ALTER TABLE {t} ADD COLUMN {col_name} {col_ddl}"))
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()

        if has_table("users"):
            add_col("users", "name", "VARCHAR(120)")

        if has_table("recurring_rules"):
            add_col("recurring_rules", "start_date", "DATE")
            add_col("recurring_rules", "weekday", "INTEGER")
            add_col("recurring_rules", "day_of_month", "INTEGER")
            add_col("recurring_rules", "next_run", "DATE")
            add_col("recurring_rules", "is_active", "BOOLEAN DEFAULT TRUE")
            add_col("recurring_rules", "tipo", "VARCHAR(16)")
            add_col("recurring_rules", "valor", "NUMERIC(12,2)")
            add_col("recurring_rules", "categoria", "VARCHAR(80)")
            add_col("recurring_rules", "descricao", "TEXT")

            try:
                db.session.execute(text("""
                    UPDATE recurring_rules
                    SET start_date = COALESCE(start_date, CURRENT_DATE)
                    WHERE start_date IS NULL
                """))
                db.session.commit()
            except Exception:
                db.session.rollback()

            try:
                db.session.execute(text("""
                    UPDATE recurring_rules
                    SET next_run = COALESCE(next_run, CURRENT_DATE)
                    WHERE next_run IS NULL
                """))
                db.session.commit()
            except Exception:
                db.session.rollback()

    except Exception as e:
        print("DB bootstrap_schema failed:", repr(e))
        try:
            db.session.rollback()
        except Exception:
            pass


with app.app_context():
    _create_tables_if_needed()
    _bootstrap_schema()

# -------------------------
# Helpers
# -------------------------
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
    return jsonify(status_payload(
        db_enabled=DB_ENABLED,
        raw_db_url=_raw_db_url,
        graph_version=GRAPH_VERSION,
        wa_access_token=WA_ACCESS_TOKEN,
        wa_phone_number_id=WA_PHONE_NUMBER_ID,
        wa_verify_token=WA_VERIFY_TOKEN,
        min_password_len=MIN_PASSWORD_LEN,
        openai_api_key=OPENAI_API_KEY,
    ))


@app.get("/api/wa_link")
def api_wa_link():
    uid = get_logged_user_id()
    email = get_logged_email()

    to = normalize_wa_number(WA_PUBLIC_NUMBER)
    if not uid or not email:
        return jsonify(url=f"https://wa.me/{to}")

    text_msg = f"conectar {email}"
    url = f"https://wa.me/{to}?text={quote(text_msg)}"
    return jsonify(url=url)


@app.get("/wa")
def wa_shortcut():
    uid = get_logged_user_id()
    email = get_logged_email()
    to = normalize_wa_number(WA_PUBLIC_NUMBER)

    if uid and email:
        text_msg = f"conectar {email}"
        url = f"https://wa.me/{to}?text={quote(text_msg)}"
    else:
        url = f"https://wa.me/{to}"

    return ("", 302, {"Location": url})

# -------------------------
# Panic Reset
# -------------------------
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


@app.route("/api/panic_reset", methods=["GET", "POST"])
def api_panic_reset():
    if not _panic_allowed():
        return jsonify({"error": "forbidden"}), 403

    try:
        db.session.execute(
            text(
                "TRUNCATE TABLE processed_messages, wa_links, transactions, category_rules, wa_pending, recurring_rules, investments, users "
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
        Investment.query.delete()
        User.query.delete()
        db.session.commit()
        return jsonify({"ok": True, "message": "Banco limpo (fallback)."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "panic_reset_failed", "detail": str(e)}), 500

# -------------------------
# Auth API
# -------------------------
# Auth and account routes moved to routes/

# -------------------------
# Transactions API
# -------------------------
@app.get("/api/lancamentos")
def api_list_lancamentos():
    uid = require_login()
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
        items.append({
            "row": t.id,
            "id": t.id,
            "data": t.data.isoformat() if t.data else None,
            "tipo": t.tipo,
            "categoria": t.categoria,
            "descricao": t.descricao or "",
            "valor": float(t.valor) if t.valor is not None else 0.0,
            "origem": t.origem,
            "criado_em": t.created_at.isoformat() if t.created_at else "",
        })

    return jsonify(items=items)


@app.post("/api/lancamentos")
def api_create_lancamento():
    uid = require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    dataj = request.get_json(silent=True) or {}

    tipo = str(dataj.get("tipo") or "").strip().upper()
    if tipo not in ("RECEITA", "GASTO"):
        return jsonify(error="Tipo inválido"), 400

    descricao = str(dataj.get("descricao") or "").strip() or None
    raw_categoria = str(dataj.get("categoria") or "").strip()
    categoria = raw_categoria.title() if raw_categoria else None
    if not categoria:
        categoria = guess_category_from_text(uid, f"{raw_categoria} {descricao or ''}") or "Outros"

    d = parse_date_any(dataj.get("data"))

    try:
        valor = parse_brl_value(dataj.get("valor"))
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
    uid = require_login()
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
    t.data = parse_date_any(payload.get("data") or t.data.isoformat())
    t.categoria = (str(payload.get("categoria") or t.categoria).strip() or "Outros").title()
    t.descricao = str(payload.get("descricao") or "").strip() or None

    try:
        t.valor = parse_brl_value(payload.get("valor"))
    except ValueError as e:
        return jsonify(error=str(e)), 400

    db.session.commit()
    return jsonify(ok=True)


@app.delete("/api/lancamentos/<int:row>")
def api_delete_lancamento(row: int):
    uid = require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    t = Transaction.query.filter_by(id=row, user_id=uid).first()
    if not t:
        return jsonify(error="Sem permissão ou inexistente"), 403

    db.session.delete(t)
    db.session.commit()
    return jsonify(ok=True)

# -----------------------
# Investimentos
# -----------------------
@app.get("/api/investimentos")
def api_investimentos_list():
    user_id = require_login()
    if not user_id:
        return jsonify({"error": "Não logado"}), 401

    limit = int(request.args.get("limit", "50"))
    q = Investment.query.filter_by(user_id=user_id).order_by(Investment.data.desc(), Investment.id.desc())
    items = q.limit(min(limit, 200)).all()

    out = []
    for it in items:
        out.append({
            "id": it.id,
            "data": it.data.isoformat(),
            "ativo": it.ativo,
            "tipo": it.tipo,
            "valor": str(it.valor),
            "descricao": it.descricao or "",
        })
    return jsonify({"items": out})


@app.post("/api/investimentos")
def api_investimentos_create():
    user_id = require_login()
    if not user_id:
        return jsonify({"error": "Não logado"}), 401

    data = request.get_json(silent=True) or {}
    ativo = str(data.get("ativo") or "").strip()
    if not ativo:
        return jsonify({"error": "Informe o ativo (ex: Tesouro Selic, PETR4, BTC)."}), 400

    tipo = str(data.get("tipo") or "APORTE").strip().upper()
    if tipo not in ("APORTE", "RESGATE"):
        return jsonify({"error": "Tipo inválido. Use APORTE ou RESGATE."}), 400

    valor = parse_money_br_to_decimal(data.get("valor"))
    if valor <= 0:
        return jsonify({"error": "Informe um valor válido (> 0)."}), 400

    it = Investment(
        user_id=user_id,
        data=iso_date(data.get("data")),
        ativo=ativo,
        tipo=tipo,
        valor=valor,
        descricao=str(data.get("descricao") or "").strip() or None,
    )
    db.session.add(it)
    db.session.commit()
    return jsonify({"ok": True, "id": it.id})


@app.delete("/api/investimentos/<int:item_id>")
def api_investimentos_delete(item_id: int):
    user_id = require_login()
    if not user_id:
        return jsonify({"error": "Não logado"}), 401

    it = Investment.query.filter_by(user_id=user_id, id=item_id).first()
    if not it:
        return jsonify({"error": "Investimento não encontrado."}), 404

    db.session.delete(it)
    db.session.commit()
    return jsonify({"ok": True})

# -------------------------
# Dashboard + IA
# -------------------------
@app.get("/api/dashboard")
def api_dashboard():
    uid = require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    try:
        mes = int(request.args.get("mes"))
        ano = int(request.args.get("ano"))
    except Exception:
        return jsonify(error="Parâmetros mes/ano inválidos"), 400

    start = date(ano, mes, 1)
    end = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)

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


@app.get("/api/insights_dashboard")
def api_insights_dashboard():
    uid = require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    try:
        mes = int(request.args.get("mes", "0"))
        ano = int(request.args.get("ano", "0"))
    except Exception:
        mes = 0
        ano = 0

    today = datetime.utcnow().date()
    if not (1 <= mes <= 12):
        mes = today.month
    if ano < 2000 or ano > 3000:
        ano = today.year

    start = date(ano, mes, 1)
    end = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)

    rows = (
        Transaction.query
        .filter(Transaction.user_id == uid)
        .filter(Transaction.data >= start)
        .filter(Transaction.data < end)
        .all()
    )

    receitas = Decimal("0")
    gastos = Decimal("0")
    categorias = {}

    for t in rows:
        v = Decimal(t.valor or 0)
        if (t.tipo or "").upper() == "RECEITA":
            receitas += v
        else:
            gastos += v
            categorias[t.categoria] = categorias.get(t.categoria, Decimal("0")) + v

    score = 50
    status = "atencao"

    if receitas > 0:
        ratio = gastos / receitas
        if ratio < Decimal("0.50"):
            score = 90
            status = "saudavel"
        elif ratio < Decimal("0.70"):
            score = 80
            status = "saudavel"
        elif ratio < Decimal("0.90"):
            score = 65
            status = "atencao"
        else:
            score = 40
            status = "critico"
    elif gastos > 0:
        score = 25
        status = "critico"

    if not rows:
        insight = "Sem lançamentos no mês selecionado ainda."
        status = "atencao"
    elif gastos > receitas:
        insight = "⚠️ Seus gastos estão maiores que suas receitas neste mês."
        status = "critico"
    elif categorias:
        top = max(categorias.items(), key=lambda x: x[1])
        insight = f"Você gastou mais em {top[0]} neste mês."
    else:
        insight = "Seu controle financeiro está equilibrado."

    top_categorias = sorted(categorias.items(), key=lambda x: x[1], reverse=True)

    return jsonify(
        score=score,
        status=status,
        insight=insight,
        categorias=[c[0] for c in top_categorias],
        valores=[float(c[1]) for c in top_categorias],
        receitas=float(receitas),
        gastos=float(gastos),
    )


@app.get("/api/projecao")
def api_projecao():
    uid = require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    p = calc_projection(uid)
    return jsonify({
        "saldo_atual": float(p["saldo_atual"]),
        "receitas_recorrentes_futuras": float(p["receitas_recorrentes_futuras"]),
        "gastos_recorrentes_futuros": float(p["gastos_recorrentes_futuros"]),
        "gasto_medio_diario": float(p["gasto_medio_diario"]),
        "estimativa_gastos_restantes": float(p["estimativa_gastos_restantes"]),
        "saldo_previsto": float(p["saldo_previsto"]),
        "dias_restantes": p["dias_restantes"],
        "alerta_negativo": p["alerta_negativo"],
    })


@app.get("/api/alertas")
def api_alertas():
    uid = require_login()
    if not uid:
        return jsonify(error="Não logado"), 401
    return jsonify(items=calc_alerts(uid))


@app.get("/api/patrimonio")
def api_patrimonio():
    uid = require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    months = int(request.args.get("months", "6"))
    months = max(3, min(12, months))
    labels, values = calc_patrimonio_series(uid, months)
    return jsonify(labels=labels, values=values)


@app.route("/api/score_financeiro")
def api_score_financeiro():
    uid = require_login()
    if not uid:
        return jsonify({"error": "Não logado"}), 401

    q = Transaction.query.filter(Transaction.user_id == uid).all()

    receitas = sum(Decimal(t.valor or 0) for t in q if (t.tipo or "").upper() == "RECEITA")
    gastos = sum(Decimal(t.valor or 0) for t in q if (t.tipo or "").upper() == "GASTO")
    saldo = receitas - gastos

    score = 50
    status = "atencao"

    if receitas > 0:
        ratio = gastos / receitas
        if ratio < Decimal("0.50"):
            score = 90
            status = "saudavel"
        elif ratio < Decimal("0.70"):
            score = 80
            status = "saudavel"
        elif ratio < Decimal("0.90"):
            score = 65
            status = "atencao"
        else:
            score = 40
            status = "critico"
    elif gastos > 0:
        score = 25
        status = "critico"

    if saldo > 0 and score < 100:
        score = min(100, score + 5)

    return jsonify({
        "score": int(score),
        "status": status,
        "receitas": float(receitas),
        "gastos": float(gastos),
        "saldo": float(saldo)
    })

# --------------------------------
# WhatsApp Webhook
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
                    msg_type = msg.get("type")
                    if msg_type not in ("text", "audio", "image", "document"):
                        continue

                    msg_id = msg.get("id")
                    wa_from = normalize_wa_number(msg.get("from") or "")
                    body = ((msg.get("text") or {}) or {}).get("body", "") or ""

                    if msg_id and ProcessedMessage.query.filter_by(msg_id=msg_id).first():
                        continue
                    if msg_id:
                        db.session.add(ProcessedMessage(msg_id=msg_id, wa_from=wa_from))
                        db.session.commit()

                    parsed = parse_wa_text(body) if msg_type == "text" else {"cmd": "MEDIA", "media_type": msg_type}

                    if msg_type == "text" and parsed["cmd"] == "HELP":
                        wa_send_text(wa_from, wa_help_text())
                        continue

                    if msg_type == "text" and parsed["cmd"] == "CONNECT":
                        email = parsed.get("email")
                        if not email or "@" not in email:
                            wa_send_text(wa_from, "Email inválido. Ex: conectar david@email.com")
                            continue

                        u = get_or_create_user_by_email(User, db, email, password=None)

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
                             "Digite 'ajuda' para ver os comandos.\n"
                             "Exemplo: paguei 32,90 mercado")
                        )
                        continue

                    link = WaLink.query.filter_by(wa_from=wa_from).first()
                    if not link:
                        wa_send_text(
                            wa_from,
                            "🔒 Seu WhatsApp não está conectado.\n\nEnvie:\n"
                            "conectar SEU_EMAIL_DO_APP\n"
                            "Ex: conectar david@email.com\n\n"
                            "Depois digite: ajuda"
                        )
                        continue

                    if msg_type == "text" and _handle_pending_ai_confirmation(wa_from, link.user_id, body):
                        continue

                    if msg_type in ("audio", "image", "document"):
                        if _handle_whatsapp_media_message(link, wa_from, msg):
                            continue

                    if parsed["cmd"] == "DESFAZER":
                        limit_dt = datetime.utcnow() - timedelta(minutes=5)
                        last = (
                            Transaction.query
                            .filter(Transaction.user_id == link.user_id, Transaction.origem == "WA")
                            .order_by(Transaction.id.desc())
                            .first()
                        )
                        if not last:
                            wa_send_text(wa_from, "Não achei nenhum lançamento recente do WhatsApp para desfazer.")
                            continue
                        if last.created_at and last.created_at < limit_dt:
                            wa_send_text(wa_from, "Janela de segurança passou (5 min). Use 'ultimos' e 'apagar ID'.")
                            continue
                        txid = last.id
                        db.session.delete(last)
                        db.session.commit()
                        wa_send_text(wa_from, f"✅ Desfeito: ID {txid}")
                        continue

                    if parsed["cmd"] == "RESUMO":
                        wa_send_text(wa_from, make_resumo_text(link.user_id, parsed.get("kind") or "mes"))
                        continue

                    if parsed["cmd"] == "SALDO_MES":
                        wa_send_text(wa_from, make_resumo_text(link.user_id, "mes"))
                        continue

                    if parsed["cmd"] == "ANALISE":
                        wa_send_text(wa_from, make_analise_text(link.user_id, parsed.get("kind")))
                        continue

                    if parsed["cmd"] == "PROJECAO":
                        wa_send_text(wa_from, make_projection_text(link.user_id))
                        continue

                    if parsed["cmd"] == "ALERTAS":
                        wa_send_text(wa_from, make_alerts_text(link.user_id))
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

                        guessed = guess_category_from_text(link.user_id, payload_tx.get("raw_text", ""))
                        categoria = guessed or payload_tx.get("categoria_fallback") or "Outros"

                        ttx = Transaction(
                            user_id=link.user_id,
                            tipo=payload_tx["tipo"],
                            data=parse_date_any(payload_tx.get("data")),
                            categoria=categoria,
                            descricao=(payload_tx.get("descricao") or None),
                            valor=parse_brl_value(payload_tx.get("valor")),
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
                            f"Valor: R$ {fmt_brl(ttx.valor)}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}"
                        )
                        continue

                    if parsed["cmd"] == "CAT_HELP":
                        wa_send_text(
                            wa_from,
                            "Use assim:\n"
                            "• categoria ifood = Alimentação\n"
                            "• remover categoria ifood\n"
                            "• categorias"
                        )
                        continue

                    if parsed["cmd"] == "CAT_SET":
                        key = (parsed.get("key") or "").strip()
                        cat = (parsed.get("categoria") or "").strip()
                        if not key or not cat:
                            wa_send_text(wa_from, "Formato inválido. Ex: categoria ifood = Alimentação")
                            continue

                        key_norm = norm_word(key)
                        if len(key_norm) < 2:
                            wa_send_text(wa_from, "Chave muito curta. Ex: categoria uber = Transporte")
                            continue

                        existing = CategoryRule.query.filter_by(user_id=link.user_id, pattern=key_norm).first()
                        if existing:
                            existing.categoria = cat.title()
                            existing.priority = 10
                        else:
                            db.session.add(CategoryRule(user_id=link.user_id, pattern=key_norm, categoria=cat.title(), priority=10))
                        db.session.commit()

                        wa_send_text(wa_from, f"✅ Regra salva: '{key_norm}' => {cat.title()}")
                        continue

                    if parsed["cmd"] == "CAT_DEL":
                        key = norm_word(parsed.get("key") or "")
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
                                "Dica: o bot também tem categorias automáticas padrão."
                            )
                        else:
                            lines = ["✅ Suas regras (até 30):"]
                            for r in rules:
                                lines.append(f"• {r.pattern} => {r.categoria}")
                            wa_send_text(wa_from, "\n".join(lines))
                        continue

                    if parsed["cmd"] == "REC_ADD":
                        parts = _parse_recorrente_args(parsed.get("rest") or "")
                        if not parts:
                            wa_send_text(wa_from, "Use: recorrente mensal 5 1200 aluguel")
                            continue

                        rule, err = _create_recurring_rule(link.user_id, parsed.get("freq") or "", parts)
                        if err:
                            wa_send_text(wa_from, "❌ " + err)
                            continue

                        db.session.add(rule)
                        db.session.commit()
                        wa_send_text(
                            wa_from,
                            "✅ Recorrente criada!\n"
                            f"ID: {rule.id}\n"
                            f"Freq: {rule.freq}\n"
                            f"Próximo: {rule.next_run.isoformat()}\n"
                            f"Valor: R$ {fmt_brl(rule.valor)}\n"
                            f"Categoria: {rule.categoria}"
                        )
                        continue

                    if parsed["cmd"] == "REC_LIST":
                        rules = RecurringRule.query.filter_by(user_id=link.user_id).order_by(RecurringRule.id.desc()).limit(30).all()
                        if not rules:
                            wa_send_text(wa_from, "Você ainda não tem recorrentes. Ex: recorrente mensal 5 1200 aluguel")
                        else:
                            lines = ["🔁 Suas recorrentes (até 30):"]
                            for r in rules:
                                extra = ""
                                if r.freq == "MONTHLY":
                                    extra = f"dia {r.day_of_month}"
                                elif r.freq == "WEEKLY":
                                    inv = {v: k for k, v in WEEKDAY_MAP.items()}
                                    extra = f"{inv.get(r.weekday, 'dia')}"
                                lines.append(f"• ID {r.id} | {r.freq} {extra} | R$ {fmt_brl(r.valor)} | {r.categoria} | próximo {r.next_run.isoformat()}")
                            lines.append("\nPara remover: remover recorrente ID")
                            lines.append("Para gerar agora: rodar recorrentes")
                            wa_send_text(wa_from, "\n".join(lines))
                        continue

                    if parsed["cmd"] == "REC_DEL":
                        rid = parsed["id"]
                        r = RecurringRule.query.filter_by(id=rid, user_id=link.user_id).first()
                        if not r:
                            wa_send_text(wa_from, "Não achei essa recorrente (ou não é sua). Use: recorrentes")
                            continue
                        db.session.delete(r)
                        db.session.commit()
                        wa_send_text(wa_from, f"✅ Recorrente removida: ID {rid}")
                        continue

                    if parsed["cmd"] == "REC_RUN":
                        created = _run_recorrentes_for_user(link.user_id)
                        wa_send_text(wa_from, f"✅ Recorrentes geradas: {created} lançamento(s).")
                        continue

                    if parsed["cmd"] == "ULTIMOS":
                        txs = Transaction.query.filter(Transaction.user_id == link.user_id).order_by(Transaction.id.desc()).limit(5).all()
                        if not txs:
                            wa_send_text(wa_from, "Você ainda não tem lançamentos.")
                        else:
                            lines = ["🧾 Últimos 5 lançamentos:"]
                            for ttx in txs:
                                lines.append(f"• ID {ttx.id} | {ttx.tipo} | R$ {fmt_brl(ttx.valor)} | {ttx.categoria} | {ttx.data.isoformat()}")
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

                        ok, msg2 = _apply_edit_fields(ttx, fields)
                        if not ok:
                            wa_send_text(wa_from, f"❌ Não consegui editar: {msg2}")
                            continue

                        db.session.commit()
                        wa_send_text(
                            wa_from,
                            "✅ Editado!\n"
                            f"ID: {ttx.id}\n"
                            f"Tipo: {ttx.tipo}\n"
                            f"Valor: R$ {fmt_brl(ttx.valor)}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}"
                        )
                        continue

                    if parsed["cmd"] == "CORRIGIR_ULTIMA":
                        fields = parsed.get("fields") or {}
                        ttx = Transaction.query.filter(Transaction.user_id == link.user_id).order_by(Transaction.id.desc()).first()
                        if not ttx:
                            wa_send_text(wa_from, "Você ainda não tem lançamentos.")
                            continue

                        ok, msg2 = _apply_edit_fields(ttx, fields)
                        if not ok:
                            wa_send_text(wa_from, f"❌ Não consegui corrigir: {msg2}")
                            continue

                        db.session.commit()
                        wa_send_text(
                            wa_from,
                            "✅ Corrigido na última transação!\n"
                            f"ID: {ttx.id}\n"
                            f"Tipo: {ttx.tipo}\n"
                            f"Valor: R$ {fmt_brl(ttx.valor)}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}"
                        )
                        continue

                    if parsed["cmd"] == "TX":
                        raw_text = parsed.get("raw_text") or ""
                        guessed = guess_category_from_text(link.user_id, raw_text)
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
                                f"Valor: R$ {fmt_brl(parsed['valor'])}\n"
                                f"Categoria sugerida: {categoria}\n\n"
                                "Responda apenas com:\n"
                                "• receita\n"
                                "ou\n"
                                "• gasto"
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
                            f"Valor: R$ {fmt_brl(ttx.valor)}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}\n\n"
                            "Dica: digite 'ultimos' para ver e editar."
                        )
                        continue

                    if msg_type == "text" and looks_like_finance_question(body):
                        wa_send_text(wa_from, reply_finance_question(link.user_id, body))
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

