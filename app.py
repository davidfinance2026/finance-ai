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
from sqlalchemy import text


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

# Token para rodar recorrentes via cron (opcional; altamente recomendado)
RUN_RECURRING_TOKEN = os.getenv("RUN_RECURRING_TOKEN", "").strip()

# Segurança do "desfazer" (janela em minutos)
UNDO_WINDOW_MINUTES = int(os.getenv("WA_UNDO_WINDOW_MINUTES", "5"))


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
    categoria = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    valor = db.Column(db.Numeric(12, 2), nullable=False)
    origem = db.Column(db.String(16), nullable=False, default="APP")  # APP/WA/REC
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
    pattern = db.Column(db.String(80), nullable=False)  # keyword simples
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
    """
    Regras de lançamentos recorrentes por usuário.
    Ex: mensal dia 5, aluguel 1200, categoria Moradia.
    """
    __tablename__ = "recurring_rules"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    freq = db.Column(db.String(16), nullable=False)  # DAILY/WEEKLY/MONTHLY
    interval = db.Column(db.Integer, nullable=False, server_default=text("1"))

    # para MONTHLY
    day_of_month = db.Column(db.Integer, nullable=True)  # 1..31

    # para WEEKLY
    weekday = db.Column(db.Integer, nullable=True)  # 0=Mon..6=Sun

    tipo = db.Column(db.String(16), nullable=False)  # RECEITA/GASTO
    categoria = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    valor = db.Column(db.Numeric(12, 2), nullable=False)

    next_date = db.Column(db.Date, nullable=False, index=True)  # próxima geração
    active = db.Column(db.Boolean, nullable=False, server_default=text("true"))

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
        "undo_window_minutes": UNDO_WINDOW_MINUTES,
        "recurring_cron_protected": bool(RUN_RECURRING_TOKEN),
    }


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
# Transactions API (ALINHADO COM SEU index.html)
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

    data = request.get_json(silent=True) or {}

    tipo = str(data.get("tipo") or "").strip().upper()
    if tipo not in ("RECEITA", "GASTO"):
        return jsonify(error="Tipo inválido"), 400

    categoria = (str(data.get("categoria") or "").strip() or "Outros").title()
    descricao = str(data.get("descricao") or "").strip() or None
    d = _parse_date_any(data.get("data"))

    try:
        valor = _parse_brl_value(data.get("valor"))
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


# -------------------------
# WhatsApp - Inteligência + Opções 1/2/3
# -------------------------
CONNECT_ALIASES = ("conectar", "vincular", "linkar", "associar", "registrar", "conexao", "conexão")

NEGATIONS = {"nao", "não", "nunca", "jamais"}

# valor com milhares + decimais: 1.234,56 / 1234,56 / 1234.56 / 45 / 45,90
VALUE_RE = re.compile(r"([+\-])?\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:[.,]\d{1,2})?)")

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

# Categorias automáticas padrão
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
    """
    Retorna (tipo, confidence) onde confidence:
      - "high" => confiante
      - "low"  => dúvida (pede confirmação)
    """
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

    # 1) personalizadas do usuário
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

    # 2) padrão
    for cat, keys in DEFAULT_CATEGORY_KEYWORDS:
        nkeys = {_norm_word(k) for k in keys}
        if tokens & nkeys:
            return cat

    return None


# ----- comandos WhatsApp -----
CMD_HELP_RE = re.compile(r"^\s*(ajuda|\?|help)\s*$", re.IGNORECASE)
CMD_ULTIMOS_RE = re.compile(r"^\s*ultimos\s*$", re.IGNORECASE)
CMD_APAGAR_RE = re.compile(r"^\s*apagar\s+(\d+)\s*$", re.IGNORECASE)
CMD_CORRIGIR_ULTIMA_RE = re.compile(r"^\s*corrigir\s+ultima\s+(.+)$", re.IGNORECASE)
CMD_EDITAR_RE = re.compile(r"^\s*editar\s+(\d+)\s+(.+)$", re.IGNORECASE)

# Opção 1: desfazer
CMD_DESFAZER_RE = re.compile(r"^\s*desfazer\s*$", re.IGNORECASE)

# Opção 2: resumo
CMD_RESUMO_RE = re.compile(r"^\s*(resumo|saldo)\s*(hoje|dia|semana|m[eê]s)\s*$", re.IGNORECASE)

# categorias via WhatsApp
CAT_SET_RE = re.compile(r"^\s*categoria\s+(.+?)\s*=\s*(.+?)\s*$", re.IGNORECASE)
CAT_DEL_RE = re.compile(r"^\s*remover\s+categoria\s+(.+?)\s*$", re.IGNORECASE)
CAT_LIST_RE = re.compile(r"^\s*categorias\s*$", re.IGNORECASE)

# Opção 3: recorrentes
CMD_REC_ADD_RE = re.compile(r"^\s*(recorrente|todo)\s+(diari[ao]|semanal|mensal)\s+(.+)\s*$", re.IGNORECASE)
CMD_RECS_LIST_RE = re.compile(r"^\s*recorrentes\s*$", re.IGNORECASE)
CMD_REC_DEL_RE = re.compile(r"^\s*remover\s+recorrente\s+(\d+)\s*$", re.IGNORECASE)
CMD_REC_RUN_RE = re.compile(r"^\s*(gerar\s+recorrentes|rodar\s+recorrentes)\s*$", re.IGNORECASE)


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
        "🧠 Se houver dúvida, eu pergunto: RECEITA ou GASTO.\n"
        "Responda apenas: receita  (ou)  gasto\n\n"
        "✏️ Corrigir aqui:\n"
        "• ultimos\n"
        "• apagar 123\n"
        "• editar 123 valor=35,90 categoria=Alimentação data=2026-03-01 descricao=\"algo\" tipo=receita\n"
        "• corrigir ultima categoria=Transporte\n\n"
        "↩️ Desfazer (janela de segurança):\n"
        "• desfazer\n\n"
        "📊 Resumos:\n"
        "• resumo hoje\n"
        "• resumo semana\n"
        "• resumo mês\n"
        "• saldo mês\n\n"
        "🔁 Recorrentes:\n"
        "• recorrente mensal 5 1200 aluguel\n"
        "• recorrente semanal seg 50 academia\n"
        "• recorrente diario 10 cafe\n"
        "• recorrentes\n"
        "• remover recorrente 7\n"
        "• gerar recorrentes\n\n"
        "🏷️ Ensinar categorias:\n"
        "• categorias\n"
        "• categoria ifood = Alimentação\n"
        "• remover categoria ifood\n"
    )


def _parse_kv_assignments(s: str) -> dict:
    """
    Lê pares tipo: valor=35,90 categoria=Alimentação data=2026-03-01 descricao=abc tipo=receita
    Observação: descrição pode conter espaços se você usar aspas:
      descricao="compra no mercado"
    """
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


# -------------------------
# Resumos (Opção 2)
# -------------------------
def _sum_range(user_id: int, start_d: date, end_exclusive: date):
    txs = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
        .filter(Transaction.data >= start_d)
        .filter(Transaction.data < end_exclusive)
        .all()
    )
    receitas = Decimal("0")
    gastos = Decimal("0")
    for t in txs:
        v = Decimal(t.valor or 0)
        if (t.tipo or "").upper() == "RECEITA":
            receitas += v
        else:
            gastos += v
    saldo = receitas - gastos
    return receitas, gastos, saldo, len(txs)


def _format_brl(d: Decimal) -> str:
    s = f"{d:.2f}"
    return s.replace(".", ",")


# -------------------------
# Recorrentes (Opção 3)
# -------------------------
WEEKDAY_MAP = {
    "seg": 0, "segunda": 0,
    "ter": 1, "terca": 1, "terça": 1,
    "qua": 2, "quarta": 2,
    "qui": 3, "quinta": 3,
    "sex": 4, "sexta": 4,
    "sab": 5, "sábado": 5, "sabado": 5,
    "dom": 6, "domingo": 6,
}


def _last_day_of_month(y: int, m: int) -> int:
    return calendar.monthrange(y, m)[1]


def _add_months(d: date, months: int, day_of_month: int | None = None) -> date:
    y = d.year + ((d.month - 1 + months) // 12)
    m = ((d.month - 1 + months) % 12) + 1
    dom = day_of_month if day_of_month else d.day
    dom = max(1, min(dom, _last_day_of_month(y, m)))
    return date(y, m, dom)


def _first_next_date(today: date, freq: str, interval: int, day_of_month: int | None, weekday: int | None) -> date:
    freq = (freq or "").upper()
    interval = max(1, int(interval or 1))

    if freq == "DAILY":
        return today + timedelta(days=1)

    if freq == "WEEKLY":
        target = weekday if weekday is not None else today.weekday()
        # próxima ocorrência do weekday (não hoje)
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7
        return today + timedelta(days=delta)

    if freq == "MONTHLY":
        dom = day_of_month if day_of_month else today.day
        # se ainda dá no mês atual (e não hoje), pega; senão próximo mês
        y, m = today.year, today.month
        last = _last_day_of_month(y, m)
        dom_adj = max(1, min(dom, last))
        cand = date(y, m, dom_adj)
        if cand <= today:
            cand = _add_months(today, 1, dom)
        return cand

    return today + timedelta(days=1)


def _advance_next_date(cur: date, freq: str, interval: int, day_of_month: int | None, weekday: int | None) -> date:
    freq = (freq or "").upper()
    interval = max(1, int(interval or 1))

    if freq == "DAILY":
        return cur + timedelta(days=interval)

    if freq == "WEEKLY":
        return cur + timedelta(days=7 * interval)

    if freq == "MONTHLY":
        return _add_months(cur, interval, day_of_month)

    return cur + timedelta(days=interval)


def run_recurring_for_user(user_id: int, until: date | None = None) -> int:
    """
    Gera lançamentos recorrentes vencidos (next_date <= until).
    Retorna quantos lançamentos foram criados.
    """
    until = until or datetime.utcnow().date()
    created = 0

    rules = (
        RecurringRule.query
        .filter(RecurringRule.user_id == user_id)
        .filter(RecurringRule.active.is_(True))
        .filter(RecurringRule.next_date <= until)
        .order_by(RecurringRule.next_date.asc(), RecurringRule.id.asc())
        .all()
    )

    for r in rules:
        safety = 0
        while r.next_date <= until:
            safety += 1
            if safety > 120:
                # evita loop infinito em caso de dados ruins
                break

            tx = Transaction(
                user_id=user_id,
                tipo=r.tipo,
                data=r.next_date,
                categoria=r.categoria,
                descricao=(r.descricao or None),
                valor=r.valor,
                origem="REC",
            )
            db.session.add(tx)
            created += 1

            r.next_date = _advance_next_date(r.next_date, r.freq, r.interval, r.day_of_month, r.weekday)

    db.session.commit()
    return created


def run_recurring_all(until: date | None = None) -> int:
    until = until or datetime.utcnow().date()
    created_total = 0

    # pega usuários que têm regra ativa vencida
    rows = (
        db.session.query(RecurringRule.user_id)
        .filter(RecurringRule.active.is_(True))
        .filter(RecurringRule.next_date <= until)
        .distinct()
        .all()
    )
    user_ids = [r[0] for r in rows]
    for uid in user_ids:
        created_total += run_recurring_for_user(uid, until=until)

    return created_total


@app.post("/api/run_recurring")
def api_run_recurring():
    """
    Endpoint para CRON (Railway/Render/etc).
    Protegido por RUN_RECURRING_TOKEN (se setado).
    """
    if RUN_RECURRING_TOKEN:
        token = request.headers.get("X-Run-Token") or request.args.get("token") or ""
        if str(token) != RUN_RECURRING_TOKEN:
            return jsonify({"error": "forbidden"}), 403

    until_s = request.args.get("until")  # opcional YYYY-MM-DD
    until = _parse_date_any(until_s) if until_s else datetime.utcnow().date()

    try:
        created = run_recurring_all(until=until)
        return jsonify({"ok": True, "created": created, "until": until.isoformat()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# -------------------------
# Parser WA
# -------------------------
def _parse_recurring_add(freq_word: str, rest: str):
    """
    Aceita exemplos:
      recorrente mensal 5 1200 aluguel
      recorrente semanal seg 50 academia
      recorrente diario 10 cafe

    Retorna dict com:
      freq, day_of_month, weekday, valor, tipo, categoria_fallback, descricao
    """
    freq_word_n = _norm_word(freq_word)
    if freq_word_n.startswith("diari"):
        freq = "DAILY"
    elif freq_word_n.startswith("seman"):
        freq = "WEEKLY"
    else:
        freq = "MONTHLY"

    low = _norm_word(rest)
    tokens = _tokenize(low)

    day_of_month = None
    weekday = None

    # mensal: tenta achar dia 1..31
    if freq == "MONTHLY":
        for t in tokens[:3]:
            if t.isdigit():
                v = int(t)
                if 1 <= v <= 31:
                    day_of_month = v
                    break

    # semanal: tenta achar weekday (seg/ter/...)
    if freq == "WEEKLY":
        for t in tokens[:3]:
            if t in WEEKDAY_MAP:
                weekday = WEEKDAY_MAP[t]
                break

    # encontra valor
    m = VALUE_RE.search(low)
    if not m:
        return None

    sign = m.group(1) or ""
    valor_raw = m.group(2)
    try:
        valor = _parse_brl_value(valor_raw)
    except Exception:
        return None

    before = (low[: m.start()] or "").strip()
    after = (low[m.end() :] or "").strip(" -–—")

    before_tokens = _tokenize(before)
    after_tokens = _tokenize(after)
    tipo, _conf = _detect_tipo_with_score(sign, before_tokens, after_tokens)

    categoria_fallback = "Outros"
    descricao = ""
    if after:
        parts = after.split(" ", 1)
        categoria_fallback = (parts[0] or "Outros").strip().title()
        descricao = parts[1].strip() if len(parts) > 1 else ""

    return {
        "freq": freq,
        "day_of_month": day_of_month,
        "weekday": weekday,
        "valor": valor,
        "tipo": tipo,
        "categoria_fallback": categoria_fallback,
        "descricao": descricao,
        "raw_text": rest,
    }


def _parse_wa_text(msg_text: str):
    """
    Retorna dict com cmd:
      CONNECT | CAT_* | HELP | ULTIMOS | APAGAR | EDITAR | CORRIGIR_ULTIMA | DESFAZER
      RESUMO | REC_ADD | REC_LIST | REC_DEL | REC_RUN
      TX | CONFIRM_TIPO | NONE
    """
    t = (msg_text or "").strip()
    if not t:
        return {"cmd": "NONE"}

    if CMD_HELP_RE.match(t):
        return {"cmd": "HELP"}

    if CMD_DESFAZER_RE.match(t):
        return {"cmd": "DESFAZER"}

    m = CMD_RESUMO_RE.match(t)
    if m:
        period = _norm_word(m.group(2))
        if period in ("dia",):
            period = "hoje"
        if period in ("mes", "mês"):
            period = "mes"
        return {"cmd": "RESUMO", "period": period}

    # recorrentes
    if CMD_RECS_LIST_RE.match(t):
        return {"cmd": "REC_LIST"}

    m = CMD_REC_DEL_RE.match(t)
    if m:
        return {"cmd": "REC_DEL", "id": int(m.group(1))}

    if CMD_REC_RUN_RE.match(t):
        return {"cmd": "REC_RUN"}

    m = CMD_REC_ADD_RE.match(t)
    if m:
        freq_word = m.group(2)
        rest = m.group(3)
        parsed = _parse_recurring_add(freq_word, rest)
        if not parsed:
            return {"cmd": "REC_HELP"}
        return {"cmd": "REC_ADD", **parsed}

    # categorias
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

    # edição/correção
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

    # confirmação simples (pendência)
    low_simple = _norm_word(t)
    if low_simple in ("receita", "gasto"):
        return {"cmd": "CONFIRM_TIPO", "tipo": "RECEITA" if low_simple == "receita" else "GASTO"}

    low = _norm_word(t)
    low = re.sub(r"\s+", " ", low).strip()

    # CONNECT
    for alias in CONNECT_ALIASES:
        if low.startswith(_norm_word(alias) + " "):
            email = t.split(" ", 1)[1].strip()
            return {"cmd": "CONNECT", "email": _normalize_email(email)}

    # TX normal
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

                    # dedup
                    if msg_id and ProcessedMessage.query.filter_by(msg_id=msg_id).first():
                        continue
                    if msg_id:
                        db.session.add(ProcessedMessage(msg_id=msg_id, wa_from=wa_from))
                        db.session.commit()

                    parsed = _parse_wa_text(body)

                    # HELP sempre disponível
                    if parsed["cmd"] == "HELP":
                        wa_send_text(wa_from, _wa_help_text())
                        continue

                    # CONNECT sem precisar estar linkado
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

                    # daqui pra baixo, precisa link
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

                    # confirma pendência (modo dúvida)
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
                            f"Valor: R$ {_format_brl(Decimal(ttx.valor))}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}",
                        )
                        continue

                    # Opção 1: DESFAZER (apaga última transação WA dentro da janela)
                    if parsed["cmd"] == "DESFAZER":
                        now = datetime.utcnow()
                        cutoff = now - timedelta(minutes=UNDO_WINDOW_MINUTES)

                        last_wa = (
                            Transaction.query
                            .filter(Transaction.user_id == link.user_id)
                            .filter(Transaction.origem == "WA")
                            .filter(Transaction.created_at >= cutoff)
                            .order_by(Transaction.id.desc())
                            .first()
                        )

                        if not last_wa:
                            wa_send_text(
                                wa_from,
                                f"ℹ️ Nada para desfazer.\n"
                                f"Eu só consigo desfazer a última transação criada via WhatsApp nos últimos {UNDO_WINDOW_MINUTES} min.",
                            )
                            continue

                        txid = last_wa.id
                        db.session.delete(last_wa)
                        db.session.commit()
                        wa_send_text(wa_from, f"↩️ Desfeito com sucesso! Apaguei a última transação WA (ID {txid}).")
                        continue

                    # Opção 2: RESUMO
                    if parsed["cmd"] == "RESUMO":
                        period = parsed.get("period") or "hoje"
                        today = datetime.utcnow().date()

                        if period == "hoje":
                            start = today
                            end = today + timedelta(days=1)
                            label = f"Hoje ({start.isoformat()})"
                        elif period == "semana":
                            start = today - timedelta(days=6)  # últimos 7 dias
                            end = today + timedelta(days=1)
                            label = f"Últimos 7 dias ({start.isoformat()} a {today.isoformat()})"
                        else:  # mes
                            start = date(today.year, today.month, 1)
                            end = date(today.year + 1, 1, 1) if today.month == 12 else date(today.year, today.month + 1, 1)
                            label = f"Mês atual ({start.isoformat()} a {(end - timedelta(days=1)).isoformat()})"

                        receitas, gastos, saldo, n = _sum_range(link.user_id, start, end)
                        wa_send_text(
                            wa_from,
                            "📊 Resumo — " + label + "\n\n"
                            f"• Entradas: R$ {_format_brl(receitas)}\n"
                            f"• Saídas:   R$ {_format_brl(gastos)}\n"
                            f"• Saldo:    R$ {_format_brl(saldo)}\n"
                            f"• Itens: {n}\n\n"
                            "Dica: use 'ultimos' para ver e corrigir lançamentos.",
                        )
                        continue

                    # categorias
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

                    # Opção 3: recorrentes
                    if parsed["cmd"] == "REC_HELP":
                        wa_send_text(
                            wa_from,
                            "Formato de recorrente:\n"
                            "• recorrente mensal 5 1200 aluguel\n"
                            "• recorrente semanal seg 50 academia\n"
                            "• recorrente diario 10 cafe\n\n"
                            "Depois:\n"
                            "• recorrentes\n"
                            "• remover recorrente 7\n"
                            "• gerar recorrentes",
                        )
                        continue

                    if parsed["cmd"] == "REC_ADD":
                        freq = parsed["freq"]
                        dom = parsed.get("day_of_month")
                        wd = parsed.get("weekday")
                        valor = parsed["valor"]
                        tipo = parsed["tipo"]

                        raw_text = parsed.get("raw_text") or ""
                        guessed = _guess_category_from_text(link.user_id, raw_text)
                        categoria = guessed or parsed.get("categoria_fallback") or "Outros"
                        descricao = parsed.get("descricao") or None

                        # primeira data a gerar (não hoje; começa na próxima ocorrência)
                        today = datetime.utcnow().date()
                        next_d = _first_next_date(today, freq, 1, dom, wd)

                        rr = RecurringRule(
                            user_id=link.user_id,
                            freq=freq,
                            interval=1,
                            day_of_month=dom if freq == "MONTHLY" else None,
                            weekday=wd if freq == "WEEKLY" else None,
                            tipo=tipo,
                            categoria=str(categoria).title(),
                            descricao=descricao,
                            valor=valor,
                            next_date=next_d,
                            active=True,
                        )
                        db.session.add(rr)
                        db.session.commit()

                        extra = ""
                        if freq == "MONTHLY":
                            extra = f"dia {rr.day_of_month}"
                        elif freq == "WEEKLY":
                            inv = {v: k for k, v in WEEKDAY_MAP.items()}
                            extra = f"{inv.get(rr.weekday, rr.weekday)}"

                        wa_send_text(
                            wa_from,
                            "✅ Recorrente criado!\n"
                            f"ID: {rr.id}\n"
                            f"Frequência: {rr.freq} {extra}\n"
                            f"Tipo: {rr.tipo}\n"
                            f"Valor: R$ {_format_brl(Decimal(rr.valor))}\n"
                            f"Categoria: {rr.categoria}\n"
                            f"Próxima: {rr.next_date.isoformat()}\n\n"
                            "Use: recorrentes  (para listar)  |  gerar recorrentes",
                        )
                        continue

                    if parsed["cmd"] == "REC_LIST":
                        recs = (
                            RecurringRule.query
                            .filter_by(user_id=link.user_id)
                            .order_by(RecurringRule.active.desc(), RecurringRule.id.desc())
                            .limit(20)
                            .all()
                        )
                        if not recs:
                            wa_send_text(
                                wa_from,
                                "Você ainda não tem recorrentes.\n\n"
                                "Exemplos:\n"
                                "• recorrente mensal 5 1200 aluguel\n"
                                "• recorrente semanal seg 50 academia\n"
                                "• recorrente diario 10 cafe",
                            )
                        else:
                            lines = ["🔁 Seus recorrentes (até 20):"]
                            for r in recs:
                                info = r.freq
                                if r.freq == "MONTHLY" and r.day_of_month:
                                    info += f" dia {r.day_of_month}"
                                if r.freq == "WEEKLY" and r.weekday is not None:
                                    info += f" wd {r.weekday}"
                                status = "ON" if r.active else "OFF"
                                lines.append(
                                    f"• ID {r.id} [{status}] | {info} | {r.tipo} | R$ {_format_brl(Decimal(r.valor))} | {r.categoria} | próxima {r.next_date.isoformat()}"
                                )
                            lines.append("\nPara remover: remover recorrente ID")
                            wa_send_text(wa_from, "\n".join(lines))
                        continue

                    if parsed["cmd"] == "REC_DEL":
                        rid = parsed["id"]
                        rr = RecurringRule.query.filter_by(id=rid, user_id=link.user_id).first()
                        if not rr:
                            wa_send_text(wa_from, "Não achei esse recorrente (ou não é seu). Use: recorrentes")
                            continue
                        db.session.delete(rr)
                        db.session.commit()
                        wa_send_text(wa_from, f"✅ Recorrente removido: ID {rid}")
                        continue

                    if parsed["cmd"] == "REC_RUN":
                        created = run_recurring_for_user(link.user_id, until=datetime.utcnow().date())
                        wa_send_text(
                            wa_from,
                            f"✅ Recorrentes gerados!\nCriados: {created}\n\n"
                            "Dica: use 'ultimos' para ver e corrigir.",
                        )
                        continue

                    # listar/apagar/editar
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
                                    f"• ID {ttx.id} | {ttx.tipo} | R$ {_format_brl(Decimal(ttx.valor))} | {ttx.categoria} | {ttx.data.isoformat()} | {ttx.origem}"
                                )
                            lines.append("\nPara editar: editar ID valor=... categoria=... data=... tipo=receita/gasto")
                            lines.append("Para apagar: apagar ID")
                            lines.append("Para desfazer (WA): desfazer")
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
                            f"Valor: R$ {_format_brl(Decimal(ttx.valor))}\n"
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
                            f"Valor: R$ {_format_brl(Decimal(ttx.valor))}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}",
                        )
                        continue

                    # TX normal (modo dúvida + categoria inteligente)
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
                                    "tipo": parsed["tipo"],  # sugestão
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
                                f"Valor: R$ {_format_brl(Decimal(parsed['valor']))}\n"
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
                            f"Valor: R$ {_format_brl(Decimal(ttx.valor))}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}\n\n"
                            "Dica: digite 'desfazer' (até 5 min), 'resumo hoje' ou 'ultimos'.",
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
