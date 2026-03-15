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
from flask import Flask, request, jsonify, send_from_directory, session, render_template, redirect
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

from budget_services import init_budget_services

from whatsapp_commands import (
    parse_wa_text,
    wa_help_text,
)

from routes.auth_routes import register_auth_routes
from routes.account_routes import register_account_routes
from routes.finance_routes import register_finance_routes
from routes.investment_routes import register_investment_routes
from routes.dashboard_routes import register_dashboard_routes
from routes.whatsapp_routes import register_whatsapp_routes
from routes.budget_routes import register_budget_routes


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
_session_secure_env = os.getenv("SESSION_SECURE")
if _session_secure_env is None:
    app.config["SESSION_COOKIE_SECURE"] = bool(os.getenv("RAILWAY_ENVIRONMENT"))
else:
    app.config["SESSION_COOKIE_SECURE"] = _session_secure_env == "1"

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


class BudgetGoal(db.Model):
    __tablename__ = "budget_goals"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    ano = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)

    categoria = db.Column(db.String(80), nullable=False, default="TOTAL")

    valor_meta = db.Column(db.Numeric(12, 2), nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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

init_budget_services(
    BudgetGoal=BudgetGoal,
    Transaction=Transaction,
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
# Helpers / Routes registradas
# -------------------------
register_auth_routes(app, db, User, MIN_PASSWORD_LEN, normalize_email, hash_password, login_user)
register_account_routes(app, db, User, get_logged_user_id, get_logged_email, require_login)
register_finance_routes(app, db, Transaction, require_login, parse_date_any, parse_brl_value, guess_category_from_text)
register_investment_routes(app, db, Investment, require_login, parse_money_br_to_decimal, iso_date)

register_dashboard_routes(
    app,
    Transaction,
    require_login,
    calc_projection,
    calc_alerts,
    calc_patrimonio_series,
    looks_like_finance_question,
    reply_finance_question,
)

register_budget_routes(
    app,
    db,
    BudgetGoal,
    require_login,
    parse_money_br_to_decimal,
)

register_whatsapp_routes(
    app=app,
    db=db,
    User=User,
    Transaction=Transaction,
    Investment=Investment,
    WaLink=WaLink,
    ProcessedMessage=ProcessedMessage,
    CategoryRule=CategoryRule,
    WaPending=WaPending,
    RecurringRule=RecurringRule,
    WA_VERIFY_TOKEN=WA_VERIFY_TOKEN,
    parse_wa_text=parse_wa_text,
    wa_help_text=wa_help_text,
    normalize_wa_number=normalize_wa_number,
    get_or_create_user_by_email=get_or_create_user_by_email,
    wa_send_text=wa_send_text,
    handle_pending_ai_confirmation=_handle_pending_ai_confirmation,
    handle_whatsapp_media_message=_handle_whatsapp_media_message,
    make_resumo_text=make_resumo_text,
    make_analise_text=make_analise_text,
    make_projection_text=make_projection_text,
    make_alerts_text=make_alerts_text,
    guess_category_from_text=guess_category_from_text,
    parse_date_any=parse_date_any,
    parse_brl_value=parse_brl_value,
    fmt_brl=fmt_brl,
    norm_word=norm_word,
    pending_get=_pending_get,
    pending_set=_pending_set,
    pending_clear=_pending_clear,
    parse_recorrente_args=_parse_recorrente_args,
    create_recurring_rule=_create_recurring_rule,
    run_recorrentes_for_user=_run_recorrentes_for_user,
    apply_edit_fields=_apply_edit_fields,
    looks_like_finance_question=looks_like_finance_question,
    reply_finance_question=reply_finance_question,
)

# -------------------------
# Static / Frontend
# -------------------------
@app.get("/")
def home():
    if not get_logged_user_id():
        return redirect("/login")
    return render_template("index.html")


@app.get("/login")
def login_page():
    if get_logged_user_id():
        return redirect("/")
    return render_template("login.html")


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
                "TRUNCATE TABLE processed_messages, wa_links, transactions, category_rules, wa_pending, recurring_rules, investments, budget_goals, users "
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
        BudgetGoal.query.delete()
        User.query.delete()
        db.session.commit()
        return jsonify({"ok": True, "message": "Banco limpo (fallback)."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "panic_reset_failed", "detail": str(e)}), 500


# -------------------------
# Entry
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
