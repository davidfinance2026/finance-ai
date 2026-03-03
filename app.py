import os
import re
import hashlib
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

from flask import Flask, request, jsonify, send_from_directory, session, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

# -------------------------
# App / Config
# -------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-me")

_raw_db_url = (os.getenv("DATABASE_URL", "") or "").strip()
if _raw_db_url.startswith("postgres://"):
    _raw_db_url = _raw_db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = _raw_db_url or "sqlite:///" + os.path.join(BASE_DIR, "local.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
    "pool_size": int(os.getenv("DB_POOL_SIZE", "3")),
    "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "2")),
}

DB_ENABLED = bool(_raw_db_url)

# Cookies de sessão (Railway/HTTPS)
# Em dev local, SESSION_COOKIE_SECURE=True pode quebrar (HTTP).
app.config["SESSION_COOKIE_SECURE"] = os.getenv("COOKIE_SECURE", "1") == "1"
app.config["SESSION_COOKIE_SAMESITE"] = os.getenv("COOKIE_SAMESITE", "Lax")

db = SQLAlchemy(app)

# -------------------------
# Models
# -------------------------
class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    # dados principais
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(64), nullable=False)
    password_set = db.Column(db.Boolean, nullable=False, server_default=text("false"))

    # opcionais (seu front envia)
    nome_apelido = db.Column(db.String(120), nullable=True)
    nome_completo = db.Column(db.String(200), nullable=True)
    telefone = db.Column(db.String(40), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Transaction(db.Model):
    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    tipo = db.Column(db.String(16), nullable=False)

    # coluna real no Postgres: data
    date = db.Column("data", db.Date, nullable=False, index=True)

    categoria = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.Text, nullable=True)

    # coluna real no Postgres: valor
    valor = db.Column("valor", db.Numeric(12, 2), nullable=False)

    origem = db.Column(db.String(16), nullable=False, default="APP")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class WaLink(db.Model):
    __tablename__ = "wa_links"

    id = db.Column(db.Integer, primary_key=True)

    # o número/ID do remetente que vem no webhook (msg["from"])
    wa_from = db.Column(db.String(40), unique=True, nullable=False, index=True)

    # NOVO: vínculo forte por user_id
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)

    # compatibilidade (e útil pra migração/backfill)
    user_email = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ProcessedMessage(db.Model):
    __tablename__ = "processed_messages"

    id = db.Column(db.Integer, primary_key=True)
    msg_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    wa_from = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


# -------------------------
# DB bootstrap + migrações leves
# -------------------------
def _run_sql(sql: str) -> None:
    try:
        db.session.execute(text(sql))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _create_tables_if_needed() -> None:
    try:
        db.create_all()
        insp = inspect(db.engine)
        table_names = set(insp.get_table_names())

        # users
        if "users" in table_names:
            _run_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_set BOOLEAN NOT NULL DEFAULT false")
            _run_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS nome_apelido VARCHAR(120)")
            _run_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS nome_completo VARCHAR(200)")
            _run_sql("ALTER TABLE users ADD COLUMN IF NOT EXISTS telefone VARCHAR(40)")

        # transactions
        if "transactions" in table_names:
            _run_sql("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS data DATE")
            _run_sql("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS valor NUMERIC(12,2)")
            _run_sql(
                """
                DO $$
                BEGIN
                  BEGIN
                    ALTER TABLE transactions ALTER COLUMN data TYPE DATE USING data::date;
                  EXCEPTION WHEN others THEN
                  END;

                  BEGIN
                    ALTER TABLE transactions ALTER COLUMN valor TYPE NUMERIC(12,2) USING valor::numeric;
                  EXCEPTION WHEN others THEN
                  END;
                END$$;
                """
            )

        # wa_links
        if "wa_links" in table_names:
            _run_sql("ALTER TABLE wa_links ADD COLUMN IF NOT EXISTS wa_from VARCHAR(40)")
            _run_sql("ALTER TABLE wa_links ADD COLUMN IF NOT EXISTS user_id INTEGER")
            _run_sql("ALTER TABLE wa_links ADD COLUMN IF NOT EXISTS user_email VARCHAR(255)")
            _run_sql("ALTER TABLE wa_links ADD COLUMN IF NOT EXISTS created_at TIMESTAMP")

            # remove linhas quebradas que violam NOT NULL / unique depois
            _run_sql("DELETE FROM wa_links WHERE wa_from IS NULL OR wa_from = ''")

            # backfill user_id a partir do email, se existir
            _run_sql(
                """
                UPDATE wa_links wl
                SET user_id = u.id
                FROM users u
                WHERE wl.user_id IS NULL
                  AND wl.user_email IS NOT NULL
                  AND lower(wl.user_email) = lower(u.email);
                """
            )

        # processed_messages
        if "processed_messages" in table_names:
            _run_sql("ALTER TABLE processed_messages ADD COLUMN IF NOT EXISTS wa_from VARCHAR(40)")

    except Exception as e:
        print("DB create_all/migrations failed:", repr(e))


with app.app_context():
    _create_tables_if_needed()

# -------------------------
# Helpers
# -------------------------
def _hash_password(pw: str) -> str:
    return hashlib.sha256((pw or "").encode("utf-8")).hexdigest()


def _get_logged_email() -> str | None:
    return session.get("user_email")


def _require_login() -> str | None:
    return _get_logged_email()


def _parse_brl_value(textv: str) -> Decimal:
    if textv is None:
        raise ValueError("valor vazio")

    s = str(textv).strip()
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


def _parse_date_any(d: str | None) -> date:
    if not d:
        return datetime.utcnow().date()
    s = str(d).strip()
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return datetime.strptime(s, "%Y-%m-%d").date()
        if re.match(r"^\d{2}/\d{2}/\d{4}$", s):
            return datetime.strptime(s, "%d/%m/%Y").date()
    except Exception:
        pass
    return datetime.utcnow().date()


def _safe_date_iso(v) -> str:
    if v is None:
        return datetime.utcnow().date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    try:
        return _parse_date_any(str(v)).isoformat()
    except Exception:
        return datetime.utcnow().date().isoformat()


def _get_or_create_user(email: str, password: str | None = None, extra: dict | None = None) -> User:
    email = (email or "").strip().lower()
    user = User.query.filter_by(email=email).first()
    if user:
        # atualiza extras se vierem
        if extra:
            if extra.get("nome_apelido"):
                user.nome_apelido = extra.get("nome_apelido")
            if extra.get("nome_completo"):
                user.nome_completo = extra.get("nome_completo")
            if extra.get("telefone"):
                user.telefone = extra.get("telefone")
            db.session.commit()
        return user

    if password is None:
        pw_hash = _hash_password(os.urandom(16).hex())
        user = User(email=email, password_hash=pw_hash, password_set=False)
    else:
        pw_hash = _hash_password(password)
        user = User(email=email, password_hash=pw_hash, password_set=True)

    if extra:
        user.nome_apelido = extra.get("nome_apelido") or None
        user.nome_completo = extra.get("nome_completo") or None
        user.telefone = extra.get("telefone") or None

    db.session.add(user)
    db.session.commit()
    return user


def _status_payload():
    return {"ok": True, "db_enabled": DB_ENABLED, "db_uri_set": bool(_raw_db_url)}


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
# Auth API
# -------------------------
def _extract_password_fields(data: dict) -> tuple[str, str]:
    """
    Retorna (password, confirm) aceitando nomes:
    - password / confirmPassword
    - senha / confirmarSenha
    """
    password = (data.get("password") or data.get("senha") or "").strip()
    confirm = (data.get("confirmPassword") or data.get("confirmarSenha") or data.get("confirm") or "").strip()
    return password, confirm


@app.post("/api/register")
def api_register():
    data = request.get_json(silent=True) or {}

    email = (data.get("email") or "").strip().lower()
    password, confirm = _extract_password_fields(data)

    # extras do seu front
    extra = {
        "nome_apelido": (data.get("nome_apelido") or data.get("nome") or data.get("apelido") or "").strip() or None,
        "nome_completo": (data.get("nome_completo") or data.get("nomeCompleto") or "").strip() or None,
        "telefone": (data.get("telefone") or data.get("phone") or "").strip() or None,
    }

    if not email or "@" not in email:
        return jsonify({"error": "Email inválido"}), 400

    # alinhar com seu front (mínimo 6)
    if len(password) < 6:
        return jsonify({"error": "Senha deve ter no mínimo 6 caracteres"}), 400
    if confirm and password != confirm:
        return jsonify({"error": "Senhas não conferem"}), 400

    existing = User.query.filter_by(email=email).first()
    if existing:
        if getattr(existing, "password_set", False) is False:
            existing.password_hash = _hash_password(password)
            existing.password_set = True
            # salva extras
            if extra.get("nome_apelido"):
                existing.nome_apelido = extra.get("nome_apelido")
            if extra.get("nome_completo"):
                existing.nome_completo = extra.get("nome_completo")
            if extra.get("telefone"):
                existing.telefone = extra.get("telefone")
            db.session.commit()

            session["user_email"] = email
            return jsonify({"ok": True, "email": email, "claimed": True})
        return jsonify({"error": "Email já cadastrado"}), 409

    user = _get_or_create_user(email, password=password, extra=extra)
    session["user_email"] = user.email
    return jsonify({"ok": True, "email": user.email})


@app.post("/api/reset_password")
def api_reset_password():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    new_password = (
        (data.get("newPassword") or data.get("password") or data.get("senha") or "").strip()
    )
    confirm = (data.get("confirmPassword") or data.get("confirmarSenha") or "").strip()

    if not email or "@" not in email:
        return jsonify({"error": "Email inválido"}), 400
    if len(new_password) < 6:
        return jsonify({"error": "Senha deve ter no mínimo 6 caracteres"}), 400
    if confirm and new_password != confirm:
        return jsonify({"error": "Senhas não conferem"}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"error": "Usuário não encontrado"}), 404

    user.password_hash = _hash_password(new_password)
    user.password_set = True
    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or data.get("senha") or "").strip()

    user = User.query.filter_by(email=email).first()
    if not user or user.password_hash != _hash_password(password):
        return jsonify({"error": "Credenciais inválidas"}), 401

    session["user_email"] = email
    return jsonify({"ok": True, "email": email})


@app.post("/api/logout")
def api_logout():
    session.pop("user_email", None)
    return jsonify({"ok": True})


@app.get("/api/me")
def api_me():
    return jsonify({"email": _get_logged_email()})


# -------------------------
# Transactions API
# -------------------------
@app.get("/api/lancamentos")
def api_list_lancamentos():
    email = _require_login()
    if not email:
        return jsonify({"error": "Você precisa estar logado."}), 401

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"items": []})

    limit = int(request.args.get("limit", 30))
    limit = max(1, min(limit, 200))

    rows = (
        Transaction.query.filter_by(user_id=user.id)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(limit)
        .all()
    )

    items = []
    for t in rows:
        items.append(
            {
                "id": t.id,
                "tipo": t.tipo,
                "data": _safe_date_iso(t.date),
                "categoria": t.categoria,
                "descricao": t.descricao or "",
                "valor": float(t.valor) if t.valor is not None else 0.0,
                "origem": t.origem,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
        )

    return jsonify({"items": items})


@app.post("/api/lancamentos")
def api_create_lancamento():
    email = _require_login()
    if not email:
        return jsonify({"error": "Você precisa estar logado."}), 401

    data = request.get_json(silent=True) or {}

    tipo = (data.get("tipo") or "").strip().upper()
    if tipo not in ("RECEITA", "GASTO"):
        return jsonify({"error": "Tipo inválido"}), 400

    categoria = (data.get("categoria") or "").strip() or "Outros"
    descricao = (data.get("descricao") or "").strip() or None
    d = _parse_date_any(data.get("data"))

    try:
        valor = _parse_brl_value(data.get("valor"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    user = _get_or_create_user(email)

    t = Transaction(
        user_id=user.id,
        tipo=tipo,
        date=d,
        categoria=categoria,
        descricao=descricao,
        valor=valor,
        origem="APP",
    )
    db.session.add(t)
    db.session.commit()

    return jsonify({"ok": True, "id": t.id})


@app.delete("/api/lancamentos/<int:tx_id>")
def api_delete_lancamento(tx_id: int):
    email = _require_login()
    if not email:
        return jsonify({"error": "Você precisa estar logado."}), 401

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"error": "Usuário não encontrado"}), 404

    t = Transaction.query.filter_by(id=tx_id, user_id=user.id).first()
    if not t:
        return jsonify({"error": "Lançamento não encontrado"}), 404

    db.session.delete(t)
    db.session.commit()
    return jsonify({"ok": True})


@app.get("/api/dashboard")
def api_dashboard():
    email = _require_login()
    if not email:
        return jsonify({"error": "Você precisa estar logado."}), 401

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"receitas": 0.0, "gastos": 0.0, "saldo": 0.0})

    try:
        ano = int(request.args.get("ano", datetime.utcnow().year))
        mes = int(request.args.get("mes", datetime.utcnow().month))
    except Exception:
        ano = datetime.utcnow().year
        mes = datetime.utcnow().month

    start = date(ano, mes, 1)
    end = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)

    q = (
        Transaction.query.filter(Transaction.user_id == user.id)
        .filter(Transaction.date >= start)
        .filter(Transaction.date < end)
    )

    receitas = Decimal("0")
    gastos = Decimal("0")
    for t in q.all():
        if t.tipo == "RECEITA":
            receitas += Decimal(t.valor or 0)
        else:
            gastos += Decimal(t.valor or 0)

    saldo = receitas - gastos

    return jsonify(
        {"receitas": float(receitas), "gastos": float(gastos), "saldo": float(saldo), "mes": mes, "ano": ano}
    )


# -------------------------
# WhatsApp link helpers (PWA)
# -------------------------
def _mask_wa(v: str) -> str:
    if not v:
        return ""
    s = str(v)
    if len(s) <= 4:
        return "****"
    return s[:2] + "****" + s[-2:]


@app.get("/api/wa/status")
def api_wa_status():
    """
    PWA chama isso pra saber se o usuário logado já está vinculado a um wa_from.
    """
    email = _require_login()
    if not email:
        return jsonify({"error": "Você precisa estar logado."}), 401

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"linked": False})

    link = WaLink.query.filter_by(user_id=user.id).first()
    if not link:
        # fallback por email (caso antigo)
        link = WaLink.query.filter(WaLink.user_email.isnot(None)).filter(text("lower(user_email) = :e")).params(e=email).first()

    if not link:
        return jsonify({"linked": False})

    return jsonify({"linked": True, "wa_from": _mask_wa(link.wa_from)})


@app.post("/api/wa/link")
def api_wa_link_manual():
    """
    Opcional: você pode permitir que o usuário informe o wa_from manualmente no PWA.
    (Só use se você tiver certeza do valor. O melhor é o usuário mandar o email no WhatsApp.)
    """
    email = _require_login()
    if not email:
        return jsonify({"error": "Você precisa estar logado."}), 401

    data = request.get_json(silent=True) or {}
    wa_from = (data.get("wa_from") or data.get("numero") or "").strip()

    if not wa_from:
        return jsonify({"error": "wa_from inválido"}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"error": "Usuário não encontrado"}), 404

    link = WaLink.query.filter_by(wa_from=wa_from).first()
    if not link:
        link = WaLink(wa_from=wa_from, user_id=user.id, user_email=user.email)
        db.session.add(link)
    else:
        link.user_id = user.id
        link.user_email = user.email

    db.session.commit()
    return jsonify({"ok": True, "linked": True, "wa_from": _mask_wa(link.wa_from)})


# -------------------------
# WhatsApp Cloud API Webhook
# -------------------------
WA_VERIFY_TOKEN = (os.getenv("WA_VERIFY_TOKEN", "") or "").strip()


def _parse_wa_text_to_transaction(msg_text: str):
    textv = (msg_text or "").strip()
    if not textv:
        return None

    lower = textv.lower()
    parts = re.split(r"\s+", lower)
    if not parts:
        return None

    # se for só um email -> comando de link
    if len(parts) == 1 and "@" in parts[0] and "." in parts[0]:
        return {"cmd": "LINK_EMAIL", "email": parts[0]}

    tipo = None
    if parts[0] in ("gasto", "despesa", "saida", "saída"):
        tipo = "GASTO"
        parts = parts[1:]
    elif parts[0] in ("receita", "entrada"):
        tipo = "RECEITA"
        parts = parts[1:]

    if tipo is None and parts and parts[0] in ("salario", "salário"):
        tipo = "RECEITA"

    if tipo is None:
        tipo = "GASTO"

    value_token = None
    rest = []
    for p in parts:
        if value_token is None and re.search(r"\d", p):
            value_token = p
        else:
            rest.append(p)

    if not value_token:
        return None

    try:
        valor = _parse_brl_value(value_token)
    except Exception:
        return None

    categoria = (rest[0] if rest else "Outros").capitalize()
    descricao = " ".join(rest[1:]).strip() if len(rest) > 1 else None

    return {
        "cmd": "TX",
        "tipo": tipo,
        "valor": valor,
        "categoria": categoria,
        "descricao": descricao,
        "data": datetime.utcnow().date(),
    }


@app.get("/webhooks/whatsapp")
def wa_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token and token == WA_VERIFY_TOKEN:
        return challenge or "", 200

    return "Forbidden", 403


@app.post("/webhooks/whatsapp")
def wa_webhook():
    payload = request.get_json(silent=True) or {}

    try:
        entry = (payload.get("entry") or [])[0]
        changes = (entry.get("changes") or [])[0]
        value = changes.get("value") or {}
        messages = value.get("messages") or []
        if not messages:
            return jsonify({"ok": True})

        msg = messages[0]
        msg_id = msg.get("id")
        wa_from = msg.get("from")
        msg_text = ((msg.get("text") or {}).get("body") or "").strip()

        # evita gravar link sem wa_from (isso foi o seu erro no log)
        if not wa_from:
            return jsonify({"ok": True})

        # idempotência
        if msg_id and ProcessedMessage.query.filter_by(msg_id=msg_id).first():
            return jsonify({"ok": True})

        if msg_id:
            db.session.add(ProcessedMessage(msg_id=msg_id, wa_from=wa_from))
            db.session.commit()

        parsed = _parse_wa_text_to_transaction(msg_text)
        if not parsed:
            return jsonify({"ok": True})

        # 1) usuário mandou EMAIL: cria/acha user e cria/atualiza WaLink com user_id
        if parsed.get("cmd") == "LINK_EMAIL":
            email = (parsed.get("email") or "").strip().lower()
            if email and "@" in email:
                user = _get_or_create_user(email)
                link = WaLink.query.filter_by(wa_from=wa_from).first()
                if not link:
                    link = WaLink(wa_from=wa_from, user_id=user.id, user_email=user.email)
                    db.session.add(link)
                else:
                    link.user_id = user.id
                    link.user_email = user.email
                db.session.commit()
            return jsonify({"ok": True})

        # 2) lançamento: acha WaLink e usa user_id
        link = WaLink.query.filter_by(wa_from=wa_from).first()
        if not link:
            return jsonify({"ok": True})

        user_id = link.user_id
        if not user_id and link.user_email:
            user = _get_or_create_user(link.user_email)
            link.user_id = user.id
            db.session.commit()
            user_id = user.id

        if not user_id:
            return jsonify({"ok": True})

        t = Transaction(
            user_id=user_id,
            tipo=parsed["tipo"],
            date=parsed["data"],
            categoria=parsed["categoria"],
            descricao=parsed.get("descricao"),
            valor=parsed["valor"],
            origem="WA",
        )
        db.session.add(t)
        db.session.commit()
        return jsonify({"ok": True})

    except Exception as e:
        print("WA webhook error:", repr(e))
        return jsonify({"ok": True})


# -------------------------
# Panic reset (opcional)
# -------------------------
PANIC_RESET_KEY = (os.getenv("PANIC_RESET_KEY", "") or "").strip()

@app.get("/api/panic_reset")
def api_panic_reset():
    """
    Endpoint de emergência (igual o que você testou).
    Se PANIC_RESET_KEY estiver definido, exige ?key=...
    """
    if PANIC_RESET_KEY:
        key = (request.args.get("key") or "").strip()
        if key != PANIC_RESET_KEY:
            return jsonify({"error": "Forbidden"}), 403

    # se estiver logado, reseta só do usuário
    email = _get_logged_email()
    if email:
        user = User.query.filter_by(email=email).first()
        if not user:
            return jsonify({"ok": True, "scope": "user", "deleted": 0})

        deleted_tx = Transaction.query.filter_by(user_id=user.id).delete()
        WaLink.query.filter_by(user_id=user.id).delete()
        db.session.commit()
        return jsonify({"ok": True, "scope": "user", "deleted_transactions": deleted_tx})

    # admin/global
    deleted_tx = Transaction.query.delete()
    deleted_links = WaLink.query.delete()
    deleted_msgs = ProcessedMessage.query.delete()
    db.session.commit()
    return jsonify({"ok": True, "scope": "global", "deleted_transactions": deleted_tx, "deleted_links": deleted_links, "deleted_msgs": deleted_msgs})


# -------------------------
# Entry
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
