# -*- coding: utf-8 -*-
import os
import re
import json
import hashlib
import calendar
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask, request, jsonify, send_from_directory, session, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, inspect
from sqlalchemy.exc import SQLAlchemyError
from urllib.parse import quote

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

# Senha mínima
MIN_PASSWORD_LEN = int(os.getenv("MIN_PASSWORD_LEN", "6"))

# WhatsApp Cloud API (Meta)
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "").strip()
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "").strip()
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "").strip()
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v20.0").strip()

# Botão de pânico (token opcional)
PANIC_TOKEN = os.getenv("PANIC_TOKEN", "").strip()

# WhatsApp público
WA_PUBLIC_NUMBER = os.getenv("WA_PUBLIC_NUMBER", "5537998675231").strip()

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


def _create_tables_if_needed():
    try:
        db.create_all()
    except Exception as e:
        print("DB create_all failed:", repr(e))


def _bootstrap_schema():
    """Migração leve/idempotente (sem Alembic)."""
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
                db.session.execute(text(
                    f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {col_name} {col_ddl}"
                ))
                db.session.commit()
                return

            if has_col(t, col_name):
                return

            try:
                db.session.execute(text(f"ALTER TABLE {t} ADD COLUMN {col_name} {col_ddl}"))
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()

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

            if dialect == "postgresql":
                if has_col("recurring_rules", "next_date"):
                    try:
                        db.session.execute(text("""
                            ALTER TABLE recurring_rules
                            ALTER COLUMN next_date DROP NOT NULL
                        """))
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

                    try:
                        db.session.execute(text("""
                            UPDATE recurring_rules
                            SET next_run = COALESCE(next_run, next_date)
                            WHERE next_run IS NULL AND next_date IS NOT NULL
                        """))
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

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
        if re.match(r"^\d{2}-\d{2}-\d{4}$", s):
            return datetime.strptime(s, "%d-%m-%Y").date()
    except Exception:
        pass
    return datetime.utcnow().date()


def _parse_money_br_to_decimal(value):
    s = str(value or "").strip()
    if not s:
        return Decimal("0")
    s = s.replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except Exception:
        return Decimal("0")


def _iso_date(value):
    s = str(value or "").strip()
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
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


def _fmt_brl(v: Decimal | float | int | None) -> str:
    try:
        d = Decimal(v or 0)
    except Exception:
        d = Decimal("0")
    s = f"{d:.2f}"
    return s.replace(".", ",")


def _month_bounds(year: int, month: int):
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _period_range(kind: str):
    today = datetime.utcnow().date()
    k = _norm_word(kind)
    if k in ("hoje", "dia"):
        start = today
        end = today + timedelta(days=1)
        label = "hoje"
        return start, end, label
    if k == "semana":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
        label = "esta semana"
        return start, end, label

    start = date(today.year, today.month, 1)
    if today.month == 12:
        end = date(today.year + 1, 1, 1)
    else:
        end = date(today.year, today.month + 1, 1)
    label = "este mês"
    return start, end, label


def _sum_period(user_id: int, start: date, end: date):
    q = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
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
    return receitas, gastos, (receitas - gastos), q


def _calc_projection(user_id: int, ref_date: date | None = None):
    today = ref_date or datetime.utcnow().date()
    start, end = _month_bounds(today.year, today.month)

    rows = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
        .filter(Transaction.data >= start)
        .filter(Transaction.data < end)
        .all()
    )

    receitas = Decimal("0")
    gastos = Decimal("0")
    gastos_variaveis = Decimal("0")

    for t in rows:
        v = Decimal(t.valor or 0)
        if (t.tipo or "").upper() == "RECEITA":
            receitas += v
        else:
            gastos += v
            if (t.origem or "").upper() != "REC":
                gastos_variaveis += v

    saldo_atual = receitas - gastos

    future_receitas_rec = Decimal("0")
    future_gastos_rec = Decimal("0")

    recurring_rules = (
        RecurringRule.query
        .filter(RecurringRule.user_id == user_id, RecurringRule.is_active == True)
        .all()
    )

    for r in recurring_rules:
        if not r.next_run:
            continue
        if today < r.next_run < end:
            val = Decimal(r.valor or 0)
            if (r.tipo or "").upper() == "RECEITA":
                future_receitas_rec += val
            else:
                future_gastos_rec += val

    days_elapsed = max(1, today.day)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_left = max(0, days_in_month - today.day)

    gasto_medio_diario = (gastos_variaveis / Decimal(days_elapsed)) if gastos_variaveis > 0 else Decimal("0")
    estimativa_gastos_restantes = gasto_medio_diario * Decimal(days_left)

    saldo_previsto = saldo_atual + future_receitas_rec - future_gastos_rec - estimativa_gastos_restantes

    return {
        "saldo_atual": saldo_atual,
        "receitas_recorrentes_futuras": future_receitas_rec,
        "gastos_recorrentes_futuros": future_gastos_rec,
        "gasto_medio_diario": gasto_medio_diario,
        "estimativa_gastos_restantes": estimativa_gastos_restantes,
        "saldo_previsto": saldo_previsto,
        "dias_restantes": days_left,
        "alerta_negativo": saldo_previsto < 0,
    }


def _calc_alerts(user_id: int, ref_date: date | None = None):
    today = ref_date or datetime.utcnow().date()
    start, end = _month_bounds(today.year, today.month)

    current_rows = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
        .filter(Transaction.data >= start)
        .filter(Transaction.data < end)
        .all()
    )

    cat_current = {}
    total_gastos = Decimal("0")

    for t in current_rows:
        if (t.tipo or "").upper() != "GASTO":
            continue
        v = Decimal(t.valor or 0)
        total_gastos += v
        cat_current[t.categoria] = cat_current.get(t.categoria, Decimal("0")) + v

    alerts = []
    projection = _calc_projection(user_id, today)

    if projection["alerta_negativo"]:
        alerts.append({
            "nivel": "alto",
            "titulo": "Saldo previsto negativo",
            "mensagem": f"Seu saldo projetado para o fim do mês é R$ {_fmt_brl(projection['saldo_previsto'])}.",
        })

    if total_gastos > 0:
        cat_top = max(cat_current.items(), key=lambda kv: kv[1])
        if (cat_top[1] / total_gastos) >= Decimal("0.45"):
            alerts.append({
                "nivel": "medio",
                "titulo": f"{cat_top[0]} está pesado no mês",
                "mensagem": f"{cat_top[0]} representa {(cat_top[1] / total_gastos * 100):.0f}% dos seus gastos.",
            })

    for cat, current_value in sorted(cat_current.items(), key=lambda kv: kv[1], reverse=True)[:5]:
        hist_values = []
        for i in range(1, 4):
            base_month = today.month - i
            base_year = today.year
            while base_month <= 0:
                base_month += 12
                base_year -= 1

            h_start, h_end = _month_bounds(base_year, base_month)
            rows = (
                Transaction.query
                .filter(Transaction.user_id == user_id)
                .filter(Transaction.tipo == "GASTO")
                .filter(Transaction.data >= h_start)
                .filter(Transaction.data < h_end)
                .filter(Transaction.categoria == cat)
                .all()
            )
            hist_values.append(sum(Decimal(r.valor or 0) for r in rows))

        if hist_values:
            media_hist = sum(hist_values) / Decimal(len(hist_values))
            if media_hist > 0 and current_value >= media_hist * Decimal("1.40"):
                alerts.append({
                    "nivel": "medio",
                    "titulo": f"{cat} acima da média",
                    "mensagem": f"Você gastou R$ {_fmt_brl(current_value)} em {cat}; média recente R$ {_fmt_brl(media_hist)}.",
                })

    return alerts[:5]


def _calc_patrimonio_series(user_id: int, months: int = 6):
    today = datetime.utcnow().date()
    labels = []
    values = []

    first_month = today.month - (months - 1)
    first_year = today.year
    while first_month <= 0:
        first_month += 12
        first_year -= 1

    running = Decimal("0")

    for offset in range(months):
        month = first_month + offset
        year = first_year
        while month > 12:
            month -= 12
            year += 1

        start, end = _month_bounds(year, month)

        txs = (
            Transaction.query
            .filter(Transaction.user_id == user_id)
            .filter(Transaction.data >= start)
            .filter(Transaction.data < end)
            .all()
        )
        invs = (
            Investment.query
            .filter(Investment.user_id == user_id)
            .filter(Investment.data >= start)
            .filter(Investment.data < end)
            .all()
        )

        receitas = sum(Decimal(t.valor or 0) for t in txs if (t.tipo or "").upper() == "RECEITA")
        gastos = sum(Decimal(t.valor or 0) for t in txs if (t.tipo or "").upper() == "GASTO")
        aportes = sum(Decimal(i.valor or 0) for i in invs if (i.tipo or "").upper() == "APORTE")
        resgates = sum(Decimal(i.valor or 0) for i in invs if (i.tipo or "").upper() == "RESGATE")

        running += (receitas - gastos) + (aportes - resgates)

        labels.append(f"{month:02d}/{str(year)[2:]}")
        values.append(float(running))

    return labels, values


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

    default_category_keywords = [
        ("Alimentação", {"ifood", "i-food", "restaurante", "lanchonete", "pizza", "burguer", "hamburguer", "lanche", "mercado", "padaria", "cafe", "café"}),
        ("Transporte", {"uber", "99", "taxi", "táxi", "onibus", "ônibus", "metro", "metrô", "gasolina", "etanol", "combustivel", "combustível", "estacionamento"}),
        ("Moradia", {"aluguel", "condominio", "condomínio", "iptu", "prestacao", "prestação", "financiamento", "luz", "energia", "agua", "água", "internet"}),
        ("Saúde", {"farmacia", "farmácia", "remedio", "remédio", "medico", "médico", "consulta", "exame", "dentista"}),
        ("Educação", {"curso", "faculdade", "escola", "mensalidade", "livro"}),
        ("Lazer", {"cinema", "show", "bar", "viagem", "hotel"}),
        ("Impostos", {"imposto", "taxa", "multa"}),
        ("Transferências", {"pix", "ted", "doc", "transferencia", "transferência"}),
    ]

    for cat, keys in default_category_keywords:
        nkeys = {_norm_word(k) for k in keys}
        if tokens & nkeys:
            return cat

    return None

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


@app.get("/api/wa_link")
def api_wa_link():
    uid = _get_logged_user_id()
    email = _get_logged_email()

    to = _normalize_wa_number(WA_PUBLIC_NUMBER)
    if not uid or not email:
        return jsonify(url=f"https://wa.me/{to}")

    text_msg = f"conectar {email}"
    url = f"https://wa.me/{to}?text={quote(text_msg)}"
    return jsonify(url=url)


@app.get("/wa")
def wa_shortcut():
    uid = _get_logged_user_id()
    email = _get_logged_email()
    to = _normalize_wa_number(WA_PUBLIC_NUMBER)

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

    descricao = str(dataj.get("descricao") or "").strip() or None
    raw_categoria = str(dataj.get("categoria") or "").strip()
    categoria = raw_categoria.title() if raw_categoria else None
    if not categoria:
        categoria = _guess_category_from_text(uid, f"{raw_categoria} {descricao or ''}") or "Outros"

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
    t.categoria = (str(payload.get("categoria") or t.categoria).strip() or "Outros").title()
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


# -----------------------
# Investimentos
# -----------------------
@app.get("/api/investimentos")
def api_investimentos_list():
    user_id = _require_login()
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
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Não logado"}), 401

    data = request.get_json(silent=True) or {}
    ativo = str(data.get("ativo") or "").strip()
    if not ativo:
        return jsonify({"error": "Informe o ativo (ex: Tesouro Selic, PETR4, BTC)."}), 400

    tipo = str(data.get("tipo") or "APORTE").strip().upper()
    if tipo not in ("APORTE", "RESGATE"):
        return jsonify({"error": "Tipo inválido. Use APORTE ou RESGATE."}), 400

    valor = _parse_money_br_to_decimal(data.get("valor"))
    if valor <= 0:
        return jsonify({"error": "Informe um valor válido (> 0)."}), 400

    it = Investment(
        user_id=user_id,
        data=_iso_date(data.get("data")),
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
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Não logado"}), 401

    it = Investment.query.filter_by(user_id=user_id, id=item_id).first()
    if not it:
        return jsonify({"error": "Investimento não encontrado."}), 404

    db.session.delete(it)
    db.session.commit()
    return jsonify({"ok": True})


# -------------------------
# Dashboard + IA v3
# -------------------------
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


@app.get("/api/projecao")
def api_projecao():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    p = _calc_projection(uid)
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
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401
    return jsonify(items=_calc_alerts(uid))


@app.get("/api/patrimonio")
def api_patrimonio():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    months = int(request.args.get("months", "6"))
    months = max(3, min(12, months))
    labels, values = _calc_patrimonio_series(uid, months)
    return jsonify(labels=labels, values=values)
