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

# Railway usa DATABASE_URL
_raw_db_url = (os.getenv("DATABASE_URL") or "").strip()
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

# Cookies de sessão (Railway é HTTPS). Em dev local, isso pode atrapalhar, então condiciona:
app.config["SESSION_COOKIE_SECURE"] = bool(os.getenv("COOKIE_SECURE", "1") == "1")
app.config["SESSION_COOKIE_SAMESITE"] = os.getenv("COOKIE_SAMESITE", "Lax")

# Regras
MIN_PASSWORD_LEN = int(os.getenv("MIN_PASSWORD_LEN", "6"))

# Botão de pânico
PANIC_TOKEN = (os.getenv("PANIC_TOKEN") or "").strip()


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

    tipo = db.Column(db.String(16), nullable=False)

    # Mapeia para a coluna real do Postgres: "data"
    date = db.Column("data", db.Date, nullable=False, index=True)

    categoria = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.Text, nullable=True)

    # Mapeia para a coluna real do Postgres: "valor"
    valor = db.Column("valor", db.Numeric(12, 2), nullable=False)

    origem = db.Column(db.String(16), nullable=False, default="APP")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class WaLink(db.Model):
    """
    Alinhado com user_id:
    - wa_number: número/wa_id (msg['from']) do WhatsApp
    - user_id: FK do usuário dono daquele WhatsApp
    """
    __tablename__ = "wa_links"

    id = db.Column(db.Integer, primary_key=True)
    wa_number = db.Column(db.String(40), unique=True, nullable=False, index=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # opcional (ajuda debug/migração)
    user_email = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ProcessedMessage(db.Model):
    __tablename__ = "processed_messages"

    id = db.Column(db.Integer, primary_key=True)
    msg_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    wa_number = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


def _run_sql_safe(sql: str) -> None:
    try:
        db.session.execute(text(sql))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _create_tables_and_migrate() -> None:
    """
    Cria tabelas e faz migração leve para:
    - transactions: garantir data/valor como tipos corretos
    - wa_links: garantir wa_number + user_id, e remover NOT NULL se seu banco antigo estiver travando
    - processed_messages: garantir wa_number
    """
    try:
        db.create_all()
        insp = inspect(db.engine)
        table_names = set(insp.get_table_names())

        # users
        if "users" in table_names:
            _run_sql_safe("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_set BOOLEAN NOT NULL DEFAULT false")

        # transactions
        if "transactions" in table_names:
            _run_sql_safe("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS data DATE")
            _run_sql_safe("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS valor NUMERIC(12,2)")

            _run_sql_safe(
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

        # wa_links (migração de schemas antigos)
        if "wa_links" in table_names:
            # adiciona colunas novas
            _run_sql_safe("ALTER TABLE wa_links ADD COLUMN IF NOT EXISTS wa_number VARCHAR(40)")
            _run_sql_safe("ALTER TABLE wa_links ADD COLUMN IF NOT EXISTS user_id INTEGER")
            _run_sql_safe("ALTER TABLE wa_links ADD COLUMN IF NOT EXISTS user_email VARCHAR(255)")
            _run_sql_safe("ALTER TABLE wa_links ADD COLUMN IF NOT EXISTS created_at TIMESTAMP")

            # compat: se existia wa_from, tenta copiar pra wa_number
            _run_sql_safe(
                """
                DO $$
                BEGIN
                  IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='wa_links' AND column_name='wa_from'
                  ) THEN
                    UPDATE wa_links
                      SET wa_number = COALESCE(wa_number, wa_from)
                    WHERE wa_number IS NULL;
                  END IF;
                END$$;
                """
            )

            # Se seu banco antigo tem NOT NULL em wa_number/user_id e existem linhas antigas nulas,
            # isso causa exatamente o erro do seu log. Então tentamos DROP NOT NULL.
            _run_sql_safe(
                """
                DO $$
                BEGIN
                  BEGIN
                    ALTER TABLE wa_links ALTER COLUMN wa_number DROP NOT NULL;
                  EXCEPTION WHEN others THEN
                  END;

                  BEGIN
                    ALTER TABLE wa_links ALTER COLUMN user_id DROP NOT NULL;
                  EXCEPTION WHEN others THEN
                  END;
                END$$;
                """
            )

            # Backfill user_id a partir de user_email (se tiver)
            try:
                rows = db.session.execute(
                    text("SELECT id, wa_number, user_id, user_email FROM wa_links")
                ).fetchall()

                for r in rows:
                    link_id = r[0]
                    wa_number = r[1]
                    user_id = r[2]
                    user_email = r[3]

                    # se wa_number está vazio mas existe wa_from em bancos antigos, já tentamos preencher acima.
                    # se ainda estiver vazio, não dá pra adivinhar.

                    if user_id is None and user_email:
                        email = str(user_email).strip().lower()
                        u = User.query.filter_by(email=email).first()
                        if not u:
                            # cria usuário "placeholder" sem senha (será setada no PWA ao registrar)
                            u = User(email=email, password_hash=_hash_password(os.urandom(16).hex()), password_set=False)
                            db.session.add(u)
                            db.session.commit()

                        db.session.execute(
                            text("UPDATE wa_links SET user_id = :uid WHERE id = :id"),
                            {"uid": u.id, "id": link_id},
                        )
                        db.session.commit()

                # Agora que deu pra preencher, podemos tentar voltar NOT NULL (opcional).
                # Como pode existir linha antiga sem wa_number, não forçamos aqui.

            except Exception:
                db.session.rollback()

        # processed_messages
        if "processed_messages" in table_names:
            _run_sql_safe("ALTER TABLE processed_messages ADD COLUMN IF NOT EXISTS wa_number VARCHAR(40)")

    except Exception as e:
        print("DB migrate failed:", repr(e))


with app.app_context():
    _create_tables_and_migrate()


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
    # evita crash "str has no attribute isoformat"
    if v is None:
        return datetime.utcnow().date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    try:
        return _parse_date_any(str(v)).isoformat()
    except Exception:
        return datetime.utcnow().date().isoformat()


def _get_or_create_user(email: str, password: str | None = None) -> User:
    email = (email or "").strip().lower()
    user = User.query.filter_by(email=email).first()
    if user:
        return user

    if password is None:
        pw_hash = _hash_password(os.urandom(16).hex())
        user = User(email=email, password_hash=pw_hash, password_set=False)
    else:
        pw_hash = _hash_password(password)
        user = User(email=email, password_hash=pw_hash, password_set=True)

    db.session.add(user)
    db.session.commit()
    return user


def _status_payload():
    return {"ok": True, "db_enabled": DB_ENABLED, "db_uri_set": bool(_raw_db_url)}


def _panic_allowed(req) -> bool:
    """
    Aceita token via:
    - querystring: ?token=XXX
    - header: X-Panic-Token: XXX
    """
    if not PANIC_TOKEN:
        return False
    t = (req.args.get("token") or "").strip()
    if not t:
        t = (req.headers.get("X-Panic-Token") or "").strip()
    return bool(t) and t == PANIC_TOKEN


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
# Panic reset (DB wipe)
# -------------------------
@app.get("/api/panic_reset")
def api_panic_reset():
    if not _panic_allowed(request):
        return jsonify({"error": "forbidden"}), 403

    try:
        # desloga PWA
        session.pop("user_email", None)

        if "postgresql" in (app.config["SQLALCHEMY_DATABASE_URI"] or ""):
            # Ordem e CASCADE para limpar dependências
            db.session.execute(text("TRUNCATE TABLE transactions RESTART IDENTITY CASCADE"))
            db.session.execute(text("TRUNCATE TABLE processed_messages RESTART IDENTITY CASCADE"))
            db.session.execute(text("TRUNCATE TABLE wa_links RESTART IDENTITY CASCADE"))
            db.session.execute(text("TRUNCATE TABLE users RESTART IDENTITY CASCADE"))
            db.session.commit()
        else:
            # sqlite fallback
            db.session.query(Transaction).delete()
            db.session.query(ProcessedMessage).delete()
            db.session.query(WaLink).delete()
            db.session.query(User).delete()
            db.session.commit()

        return jsonify({"ok": True, "reset": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "panic_reset_failed", "detail": str(e)}), 500


# -------------------------
# Auth API
# -------------------------
@app.post("/api/register")
def api_register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()

    if not email or "@" not in email:
        return jsonify({"error": "Email inválido"}), 400
    if len(password) < MIN_PASSWORD_LEN:
        return jsonify({"error": f"Senha deve ter no mínimo {MIN_PASSWORD_LEN} caracteres"}), 400

    existing = User.query.filter_by(email=email).first()
    if existing:
        if getattr(existing, "password_set", False) is False:
            existing.password_hash = _hash_password(password)
            existing.password_set = True
            db.session.commit()
            session["user_email"] = email
            return jsonify({"ok": True, "email": email, "claimed": True})
        return jsonify({"error": "Email já cadastrado"}), 409

    user = _get_or_create_user(email, password=password)
    session["user_email"] = email
    return jsonify({"ok": True, "email": user.email})


@app.post("/api/reset_password")
def api_reset_password():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    new_password = (data.get("newPassword") or data.get("password") or "").strip()

    if not email or "@" not in email:
        return jsonify({"error": "Email inválido"}), 400
    if len(new_password) < MIN_PASSWORD_LEN:
        return jsonify({"error": f"Senha deve ter no mínimo {MIN_PASSWORD_LEN} caracteres"}), 400

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
    password = (data.get("password") or "").strip()

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
        Transaction.query
        .filter_by(user_id=user.id)
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
        Transaction.query
        .filter(Transaction.user_id == user.id)
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
# WhatsApp Cloud API Webhook
# -------------------------
WA_VERIFY_TOKEN = (os.getenv("WA_VERIFY_TOKEN") or "").strip()


def _parse_wa_text_to_transaction(msg_text: str):
    textv = (msg_text or "").strip()
    if not textv:
        return None

    lower = textv.lower()
    parts = re.split(r"\s+", lower)
    if not parts:
        return None

    # Comando: enviar email puro para linkar
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
        wa_number = msg.get("from")  # <<< aqui é o wa_number correto
        msg_text = ((msg.get("text") or {}).get("body") or "").strip()

        # idempotência
        if msg_id and ProcessedMessage.query.filter_by(msg_id=msg_id).first():
            return jsonify({"ok": True})

        if msg_id:
            db.session.add(ProcessedMessage(msg_id=msg_id, wa_number=wa_number))
            db.session.commit()

        parsed = _parse_wa_text_to_transaction(msg_text)
        if not parsed:
            return jsonify({"ok": True})

        # LINK EMAIL -> cria/pega user e salva wa_link com user_id + wa_number
        if parsed.get("cmd") == "LINK_EMAIL":
            email = (parsed.get("email") or "").strip().lower()
            if email and wa_number:
                user = _get_or_create_user(email)  # cria placeholder se não existir

                link = WaLink.query.filter_by(wa_number=wa_number).first()
                if link:
                    link.user_id = user.id
                    link.user_email = email
                else:
                    link = WaLink(wa_number=wa_number, user_id=user.id, user_email=email)
                    db.session.add(link)

                db.session.commit()

            return jsonify({"ok": True})

        # TX -> precisa existir wa_link com user_id
        if not wa_number:
            return jsonify({"ok": True})

        link = WaLink.query.filter_by(wa_number=wa_number).first()
        if not link or not link.user_id:
            return jsonify({"ok": True})

        t = Transaction(
            user_id=link.user_id,
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
        # não quebra o webhook
        print("WA webhook error:", repr(e))
        db.session.rollback()
        return jsonify({"ok": True})


# -------------------------
# Entry
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
