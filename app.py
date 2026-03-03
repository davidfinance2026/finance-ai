import os
import re
import json
import hashlib
from datetime import datetime, date
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

# Senha mínima
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
    categoria = db.Column(db.String(80), nullable=False)
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


class CategoryMemory(db.Model):
    """
    Memória por usuário:
      keyword -> categoria (com score)
    Ex: user_id=1, keyword="farmacia" => categoria="Saúde" score=7
    """
    __tablename__ = "category_memory"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    keyword = db.Column(db.String(80), nullable=False, index=True)
    categoria = db.Column(db.String(80), nullable=False)
    score = db.Column(db.Integer, nullable=False, default=1)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "keyword", "categoria", name="uq_user_keyword_categoria"),
    )


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
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
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
            text("TRUNCATE TABLE processed_messages, wa_links, transactions, users, category_memory RESTART IDENTITY CASCADE;")
        )
        db.session.commit()
        return jsonify({"ok": True, "message": "Banco limpo: users, transactions, wa_links, processed_messages, category_memory."})
    except Exception:
        db.session.rollback()

    try:
        ProcessedMessage.query.delete()
        WaLink.query.delete()
        Transaction.query.delete()
        CategoryMemory.query.delete()
        User.query.delete()
        db.session.commit()
        return jsonify({"ok": True, "message": "Banco limpo (delete fallback)."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "panic_reset_failed", "detail": str(e)}), 500


# -------------------------
# Auth API (ALINHADO COM index.html)
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
# Transactions API (ALINHADO COM index.html)
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


def _tokenize_words(s: str) -> list[str]:
    s = (s or "").lower().strip()
    s = (
        s.replace("á", "a").replace("à", "a").replace("â", "a").replace("ã", "a")
         .replace("é", "e").replace("ê", "e")
         .replace("í", "i")
         .replace("ó", "o").replace("ô", "o").replace("õ", "o")
         .replace("ú", "u")
         .replace("ç", "c")
    )
    # mantém letras/números
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return []
    parts = [p for p in s.split(" ") if p]
    # filtra palavras muito curtas que dão ruído
    return [p for p in parts if len(p) >= 3]


# Mapa default (rápido e eficaz) - você pode ir ajustando
DEFAULT_CATEGORY_RULES: dict[str, list[str]] = {
    "Pix": ["pix", "transferencia", "transfer", "ted", "doc"],
    "Salário": ["salario", "sal", "pagamento", "prolabore", "prolab"],
    "Alimentação": ["ifood", "i-food", "restaurante", "lanche", "pizza", "hamburguer", "mercado", "supermercado", "padaria", "acougue", "feira"],
    "Transporte": ["uber", "99", "taxi", "gasolina", "combustivel", "onibus", "metro", "passagem", "estacionamento"],
    "Moradia": ["aluguel", "condominio", "condominio", "luz", "energia", "agua", "internet", "telefone", "gás", "gas", "iptu"],
    "Saúde": ["farmacia", "remedio", "medico", "consulta", "exame", "hospital", "clinica", "plano"],
    "Educação": ["curso", "faculdade", "escola", "livro", "mensalidade"],
    "Lazer": ["cinema", "show", "netflix", "spotify", "prime", "hbo", "steam", "viagem"],
    "Roupas": ["roupa", "tenis", "sapato", "camisa", "calca", "vestido"],
    "Assinaturas": ["assinatura", "mensal", "anuidade"],
    "Investimentos": ["acao", "acoes", "tesouro", "cdb", "lci", "lca", "fii", "bitcoin", "cripto", "crypto"],
    "Outros": [],
}


def _infer_category_for_user(user_id: int, text_blob: str) -> str:
    """
    Inferência híbrida:
      1) regras default (keywords -> categoria)
      2) memória do usuário (CategoryMemory) - aprende com histórico
    """
    tokens = _tokenize_words(text_blob)
    if not tokens:
        return "Outros"

    # 1) score por regras default
    scores: dict[str, int] = {}
    token_set = set(tokens)
    for cat, kws in DEFAULT_CATEGORY_RULES.items():
        if not kws:
            continue
        hit = len(token_set.intersection(set(_tokenize_words(" ".join(kws)))))
        if hit > 0:
            scores[cat] = scores.get(cat, 0) + (hit * 3)

    # 2) memória do usuário (keywords individuais)
    try:
        mem_rows = (
            CategoryMemory.query
            .filter(CategoryMemory.user_id == user_id)
            .filter(CategoryMemory.keyword.in_(tokens))
            .all()
        )
        for r in mem_rows:
            scores[r.categoria] = scores.get(r.categoria, 0) + int(r.score)
    except Exception:
        pass

    if not scores:
        return "Outros"

    # escolhe maior score
    best = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return best or "Outros"


def _learn_category_for_user(user_id: int, categoria: str, text_blob: str):
    """
    Aprende que tokens do text_blob costumam cair nessa categoria.
    Só incrementa score; idempotente por (user_id,keyword,categoria).
    """
    categoria = (categoria or "Outros").strip().title()
    tokens = _tokenize_words(text_blob)
    if not tokens:
        return

    # limita para não explodir memória
    tokens = tokens[:12]

    for kw in tokens:
        try:
            row = (
                CategoryMemory.query
                .filter_by(user_id=user_id, keyword=kw, categoria=categoria)
                .first()
            )
            if row:
                row.score = int(row.score or 0) + 1
                row.updated_at = datetime.utcnow()
            else:
                row = CategoryMemory(user_id=user_id, keyword=kw, categoria=categoria, score=1, updated_at=datetime.utcnow())
                db.session.add(row)
        except Exception:
            db.session.rollback()
            continue
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


@app.post("/api/lancamentos")
def api_create_lancamento():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    data = request.get_json(silent=True) or {}

    tipo = str(data.get("tipo") or "").strip().upper()
    if tipo not in ("RECEITA", "GASTO"):
        return jsonify(error="Tipo inválido"), 400

    # categoria do app continua mandando; se vier vazio, inferimos pela descrição
    categoria_in = str(data.get("categoria") or "").strip()
    descricao = str(data.get("descricao") or "").strip() or None
    d = _parse_date_any(data.get("data"))

    try:
        valor = _parse_brl_value(data.get("valor"))
    except ValueError as e:
        return jsonify(error=str(e)), 400

    if not categoria_in:
        categoria = _infer_category_for_user(uid, descricao or "")
    else:
        categoria = categoria_in.title()

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

    # aprende (APP também ensina)
    _learn_category_for_user(uid, categoria, f"{categoria} {descricao or ''}")

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
    cat_in = str(payload.get("categoria") or "").strip()
    t.categoria = (cat_in or t.categoria or "Outros").title()
    t.descricao = str(payload.get("descricao") or "").strip() or None

    try:
        t.valor = _parse_brl_value(payload.get("valor"))
    except ValueError as e:
        return jsonify(error=str(e)), 400

    db.session.commit()

    # aprende com edição também
    _learn_category_for_user(uid, t.categoria, f"{t.categoria} {t.descricao or ''}")

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
# WhatsApp Cloud API Webhook (parser inteligente + auto categoria)
# -------------------------
CONNECT_ALIASES = ("conectar", "vincular", "linkar", "associar", "registrar", "conexao", "conexão")

# palavras que tendem a indicar RECEITA (sem precisar escrever "receita")
INCOME_HINTS = {
    "recebi", "recebido", "recebida", "entrou", "entrada", "caiu", "credito", "crédito",
    "deposito", "depósito", "depositou", "pixrecebido", "pix_recebido",
    "salario", "salário", "comissao", "comissão", "bonus", "bônus",
    "reembolso", "refund", "ganhei", "ganho", "renda", "receita",
    "venda", "vendido", "vendeu"
}

# palavras que tendem a indicar GASTO
EXPENSE_HINTS = {
    "paguei", "pago", "pagou", "pagar", "comprei", "compra", "gastei", "gasto", "despesa",
    "saida", "saída", "saiu", "debito", "débito", "boleto", "conta", "fatura", "cartao", "cartão",
    "aluguel", "mercado", "uber", "ifood", "farmacia", "farmácia", "gasolina", "internet", "luz", "agua", "água"
}

NEGATIONS = {"nao", "não", "nunca", "jamais"}

# número com milhares + decimais: 1.234,56 / 1234,56 / 1234.56 / 45 / 45,90
VALUE_RE = re.compile(r"([+\-])?\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:[.,]\d{1,2})?)")


def _parse_wa_text(msg_text: str):
    """
    Comandos:
      conectar email@dominio.com  (também: vincular/linkar/associar/registrar)

    Lançamentos (exemplos):
      recebi 1200 salario
      entrou 50 pix joao
      paguei 32,90 mercado
      comprei 120 tenis
      + 35,90 venda camiseta
      - 18,00 uber

    Regras:
      - Se vier com + => RECEITA, com - => GASTO
      - Senão: tenta detectar por palavras (recebi/entrou => receita; paguei/comprei => gasto)
      - Senão: default GASTO
    """
    t = (msg_text or "").strip()
    if not t:
        return None

    low = t.lower().strip()
    low = re.sub(r"\s+", " ", low)

    # CONNECT
    for alias in CONNECT_ALIASES:
        if low.startswith(alias + " "):
            email = t.split(" ", 1)[1].strip()
            return {"cmd": "CONNECT", "email": _normalize_email(email)}

    # valor
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

    before_tokens = _tokenize_words(before)
    after_tokens = _tokenize_words(after)

    bset = set(before_tokens)
    aset = set(after_tokens)

    # Tipo
    if sign == "+":
        tipo = "RECEITA"
    elif sign == "-":
        tipo = "GASTO"
    else:
        b_income = len(bset.intersection(set(_tokenize_words(" ".join(INCOME_HINTS)))))
        b_exp = len(bset.intersection(set(_tokenize_words(" ".join(EXPENSE_HINTS)))))
        a_income = len(aset.intersection(set(_tokenize_words(" ".join(INCOME_HINTS)))))
        a_exp = len(aset.intersection(set(_tokenize_words(" ".join(EXPENSE_HINTS)))))

        score_income = (b_income * 3) + a_income
        score_exp = (b_exp * 3) + a_exp

        # negação no começo: "não recebi 50" -> não força receita
        has_neg = False
        if before_tokens:
            has_neg = before_tokens[0] in {"nao", "não"} or (len(before_tokens) > 1 and before_tokens[1] in {"nao", "não"})
        if has_neg and score_income > 0 and score_exp == 0:
            score_income = 0

        if score_income > score_exp and score_income > 0:
            tipo = "RECEITA"
        elif score_exp > score_income and score_exp > 0:
            tipo = "GASTO"
        else:
            tipo = "GASTO"

    # A partir daqui: categoria + descricao virão do "after" (texto depois do valor)
    # Ex: "paguei 32,90 mercado do joao" -> after="mercado do joao"
    # Se after vazio, deixa "Outros"
    raw_after = after.strip()
    descricao = raw_after

    return {
        "cmd": "TX",
        "tipo": tipo,
        "valor": valor,
        "raw_after": raw_after,   # para inferência de categoria
        "descricao": descricao,   # texto livre (pós-valor)
        "data": datetime.utcnow().date(),
    }


@app.get("/webhooks/whatsapp")
def wa_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token and token == WA_VERIFY_TOKEN:
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

                    # de-dup
                    if msg_id and ProcessedMessage.query.filter_by(msg_id=msg_id).first():
                        continue
                    if msg_id:
                        db.session.add(ProcessedMessage(msg_id=msg_id, wa_from=wa_from))
                        db.session.commit()

                    parsed = _parse_wa_text(body)
                    if not parsed:
                        wa_send_text(
                            wa_from,
                            "Não entendi 😅\n\nUse:\n"
                            "• conectar seuemail@dominio.com\n"
                            "• recebi 1200 salario\n"
                            "• entrou 50 pix joao\n"
                            "• paguei 32,90 mercado\n"
                            "• + 35,90 venda camiseta\n"
                            "• - 18,00 uber",
                        )
                        continue

                    # conectar
                    if parsed["cmd"] == "CONNECT":
                        email = parsed.get("email")
                        if not email or "@" not in email:
                            wa_send_text(wa_from, "Email inválido. Ex: conectar david@email.com")
                            continue

                        u = _get_or_create_user_by_email(email, password=None)

                        link = WaLink.query.filter_by(wa_from=wa_from).first()
                        if link:
                            link.user_id = u.id
                        else:
                            link = WaLink(wa_from=wa_from, user_id=u.id)
                            db.session.add(link)
                        db.session.commit()

                        wa_send_text(
                            wa_from,
                            f"✅ WhatsApp conectado ao email: {email}\n\n"
                            "Agora envie:\n"
                            "• recebi 1200 salario\n"
                            "• paguei 32,90 mercado\n"
                            "• + 35,90 venda camiseta\n"
                            "• - 18,00 uber",
                        )
                        continue

                    # lançar
                    link = WaLink.query.filter_by(wa_from=wa_from).first()
                    if not link:
                        wa_send_text(
                            wa_from,
                            "🔒 Seu WhatsApp não está conectado.\n\nEnvie:\nconectar SEU_EMAIL_DO_APP\n"
                            "Ex: conectar david@email.com",
                        )
                        continue

                    # categoria automática (usa texto após o valor + mensagem toda como contexto)
                    context_text = f"{parsed.get('raw_after','')} {body}"
                    categoria = _infer_category_for_user(link.user_id, context_text)

                    # descrição = texto pós-valor (mantém)
                    descricao = (parsed.get("descricao") or "").strip() or None

                    t = Transaction(
                        user_id=link.user_id,
                        tipo=parsed["tipo"],
                        data=parsed["data"],
                        categoria=categoria,
                        descricao=descricao,
                        valor=parsed["valor"],
                        origem="WA",
                    )
                    db.session.add(t)
                    db.session.commit()

                    # aprende para o futuro
                    _learn_category_for_user(link.user_id, categoria, context_text)

                    wa_send_text(
                        wa_from,
                        "✅ Lançamento salvo!\n"
                        f"Tipo: {t.tipo}\n"
                        f"Valor: R$ {str(t.valor).replace('.', ',')}\n"
                        f"Categoria: {t.categoria}\n"
                        f"Data: {t.data.isoformat()}",
                    )

    except Exception as e:
        print("WA webhook error:", repr(e))

    return "ok", 200


# -------------------------
# Entry
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
