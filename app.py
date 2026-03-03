import os
import re
import json
import decimal
from datetime import datetime, date
from functools import wraps
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras

from flask import (
    Flask, request, jsonify, session,
    send_from_directory, render_template
)
from werkzeug.security import generate_password_hash, check_password_hash


# ----------------------------
# App / Config
# ----------------------------
app = Flask(__name__, static_folder="static", template_folder="templates")

# SECRET_KEY obrigatório em produção
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não definido (Postgres).")


# ----------------------------
# DB helpers
# ----------------------------
def get_conn():
    # Railway geralmente entrega DATABASE_URL no formato postgres://
    return psycopg2.connect(DATABASE_URL, sslmode=os.environ.get("PGSSLMODE", "require"))


def init_db():
    ddl = """
    CREATE TABLE IF NOT EXISTS users (
        id              SERIAL PRIMARY KEY,
        nome_apelido    TEXT NOT NULL DEFAULT '',
        nome_completo   TEXT NOT NULL DEFAULT '',
        telefone        TEXT NOT NULL DEFAULT '',
        email           TEXT NOT NULL UNIQUE,
        password_hash   TEXT NOT NULL,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS lancamentos (
        id          SERIAL PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        data        DATE NOT NULL,
        tipo        TEXT NOT NULL CHECK (tipo IN ('RECEITA','GASTO')),
        categoria   TEXT NOT NULL DEFAULT '',
        descricao   TEXT NOT NULL DEFAULT '',
        valor       NUMERIC(14,2) NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    -- vínculo WhatsApp <-> user
    CREATE TABLE IF NOT EXISTS wa_links (
        id              SERIAL PRIMARY KEY,
        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        wa_number       TEXT NOT NULL UNIQUE,
        wa_name         TEXT NOT NULL DEFAULT '',
        last_message_at TIMESTAMPTZ,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_lanc_user_data ON lancamentos(user_id, data);
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
            conn.commit()


init_db()


# ----------------------------
# Utils
# ----------------------------
def json_error(msg, status=400):
    return jsonify({"ok": False, "error": msg}), status


def json_ok(data=None):
    payload = {"ok": True}
    if data is not None:
        payload.update(data)
    return jsonify(payload)


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        uid = session.get("user_id")
        if not uid:
            return json_error("Não autenticado", 401)
        return fn(*args, **kwargs)
    return wrapper


def current_user_id():
    return session.get("user_id")


def normalize_phone(s: str) -> str:
    if not s:
        return ""
    # mantém só dígitos
    return re.sub(r"\D+", "", s)


def looks_like_email(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})", text)
    return m.group(1).lower() if m else None


def parse_decimal(value):
    """
    Aceita:
      - 123.45
      - "123.45"
      - "123,45"
      - "1.234,56"
      - "1,234.56"
    """
    if value is None:
        raise ValueError("valor ausente")

    if isinstance(value, (int, float, decimal.Decimal)):
        return decimal.Decimal(str(value)).quantize(decimal.Decimal("0.01"))

    s = str(value).strip()
    if not s:
        raise ValueError("valor vazio")

    # remove espaços
    s = s.replace(" ", "")

    # heurística separador
    if "," in s and "." in s:
        # se a última vírgula vier depois do último ponto → vírgula decimal (pt-BR)
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        s = s.replace(",", ".")

    d = decimal.Decimal(s).quantize(decimal.Decimal("0.01"))
    return d


# ----------------------------
# Pages (PWA)
# ----------------------------
@app.get("/")
def home():
    return render_template("index.html")


@app.get("/offline")
def offline():
    return render_template("offline.html")


@app.get("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(app.static_folder, filename)


# ----------------------------
# Auth API
# ----------------------------
@app.post("/api/register")
def api_register():
    data = request.get_json(silent=True) or {}

    nome_apelido = (data.get("nome_apelido") or "").strip()
    nome_completo = (data.get("nome_completo") or "").strip()
    telefone = normalize_phone(data.get("telefone") or "")
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha") or ""
    confirmar = data.get("confirmar_senha") or ""

    if not email or "@" not in email:
        return json_error("E-mail inválido", 400)

    if len(senha) < 6:
        return json_error("Senha deve ter no mínimo 6 caracteres", 400)

    if senha != confirmar:
        return json_error("As senhas não conferem", 400)

    pw_hash = generate_password_hash(senha)

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO users (nome_apelido, nome_completo, telefone, email, password_hash)
                    VALUES (%s,%s,%s,%s,%s)
                    RETURNING id, email
                    """,
                    (nome_apelido, nome_completo, telefone, email, pw_hash),
                )
                row = cur.fetchone()
                conn.commit()

        session["user_id"] = int(row["id"])
        return jsonify({"ok": True, "email": row["email"], "user_id": row["id"]})
    except psycopg2.errors.UniqueViolation:
        return json_error("E-mail já cadastrado", 400)
    except Exception as e:
        return json_error(f"Erro ao cadastrar: {str(e)}", 400)


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha") or ""

    if not email or not senha:
        return json_error("Informe e-mail e senha", 400)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, email, password_hash FROM users WHERE email=%s", (email,))
            u = cur.fetchone()

    if not u or not check_password_hash(u["password_hash"], senha):
        return json_error("Credenciais inválidas", 401)

    session["user_id"] = int(u["id"])
    return jsonify({"ok": True, "email": u["email"], "user_id": u["id"]})


@app.post("/api/logout")
def api_logout():
    session.clear()
    return json_ok()


@app.get("/api/me")
@login_required
def api_me():
    uid = current_user_id()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, email, nome_apelido, nome_completo, telefone FROM users WHERE id=%s", (uid,))
            u = cur.fetchone()
    return jsonify({"ok": True, "user": u})


# ----------------------------
# Lancamentos API
# ----------------------------
@app.get("/api/lancamentos")
@login_required
def api_lancamentos_list():
    uid = current_user_id()
    limit = int(request.args.get("limit", "50"))
    limit = max(1, min(limit, 200))

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, data, tipo, categoria, descricao, valor
                FROM lancamentos
                WHERE user_id=%s
                ORDER BY data DESC, id DESC
                LIMIT %s
                """,
                (uid, limit),
            )
            rows = cur.fetchall()

    # serializa Decimal
    for r in rows:
        if isinstance(r.get("valor"), decimal.Decimal):
            r["valor"] = float(r["valor"])

    return jsonify({"ok": True, "items": rows})


@app.post("/api/lancamentos")
@login_required
def api_lancamentos_create():
    uid = current_user_id()
    data = request.get_json(silent=True) or {}

    try:
        data_str = (data.get("data") or "").strip()
        if not data_str:
            return json_error("data obrigatória (YYYY-MM-DD)", 400)
        d = datetime.strptime(data_str, "%Y-%m-%d").date()

        tipo = (data.get("tipo") or "").strip().upper()
        if tipo not in ("RECEITA", "GASTO"):
            return json_error("tipo deve ser RECEITA ou GASTO", 400)

        categoria = (data.get("categoria") or "").strip()
        descricao = (data.get("descricao") or "").strip()
        valor = parse_decimal(data.get("valor"))

        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO lancamentos (user_id, data, tipo, categoria, descricao, valor)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    (uid, d, tipo, categoria, descricao, valor),
                )
                row = cur.fetchone()
                conn.commit()

        return jsonify({"ok": True, "id": row["id"]})
    except ValueError as e:
        return json_error(str(e), 400)
    except Exception as e:
        return json_error(f"Erro ao salvar: {str(e)}", 400)


@app.put("/api/lancamentos/<int:item_id>")
@login_required
def api_lancamentos_update(item_id):
    uid = current_user_id()
    data = request.get_json(silent=True) or {}

    fields = []
    values = []

    if "data" in data:
        d = datetime.strptime((data.get("data") or "").strip(), "%Y-%m-%d").date()
        fields.append("data=%s")
        values.append(d)

    if "tipo" in data:
        tipo = (data.get("tipo") or "").strip().upper()
        if tipo not in ("RECEITA", "GASTO"):
            return json_error("tipo deve ser RECEITA ou GASTO", 400)
        fields.append("tipo=%s")
        values.append(tipo)

    if "categoria" in data:
        fields.append("categoria=%s")
        values.append((data.get("categoria") or "").strip())

    if "descricao" in data:
        fields.append("descricao=%s")
        values.append((data.get("descricao") or "").strip())

    if "valor" in data:
        fields.append("valor=%s")
        values.append(parse_decimal(data.get("valor")))

    if not fields:
        return json_error("Nada para atualizar", 400)

    values.extend([uid, item_id])

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE lancamentos
                SET {", ".join(fields)}
                WHERE user_id=%s AND id=%s
                """,
                tuple(values),
            )
            if cur.rowcount == 0:
                return json_error("Lançamento não encontrado", 404)
            conn.commit()

    return json_ok()


@app.delete("/api/lancamentos/<int:item_id>")
@login_required
def api_lancamentos_delete(item_id):
    uid = current_user_id()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM lancamentos WHERE user_id=%s AND id=%s", (uid, item_id))
            if cur.rowcount == 0:
                return json_error("Lançamento não encontrado", 404)
            conn.commit()
    return json_ok()


# ----------------------------
# Dashboard API
# ----------------------------
@app.get("/api/dashboard")
@login_required
def api_dashboard():
    uid = current_user_id()

    mes = int(request.args.get("mes", str(date.today().month)))
    ano = int(request.args.get("ano", str(date.today().year)))
    mes = max(1, min(mes, 12))

    start = date(ano, mes, 1)
    # calcula fim do mês
    if mes == 12:
        end = date(ano + 1, 1, 1)
    else:
        end = date(ano, mes + 1, 1)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN tipo='RECEITA' THEN valor ELSE 0 END), 0) AS receitas,
                  COALESCE(SUM(CASE WHEN tipo='GASTO' THEN valor ELSE 0 END), 0) AS gastos
                FROM lancamentos
                WHERE user_id=%s AND data >= %s AND data < %s
                """,
                (uid, start, end),
            )
            receitas, gastos = cur.fetchone()

    receitas = float(receitas)
    gastos = float(gastos)
    saldo = float(decimal.Decimal(str(receitas)) - decimal.Decimal(str(gastos)))

    return jsonify({"ok": True, "receitas": receitas, "gastos": gastos, "saldo": saldo})


# ----------------------------
# Panic reset
# ----------------------------
@app.post("/api/panic_reset")
@login_required
def api_panic_reset():
    uid = current_user_id()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM lancamentos WHERE user_id=%s", (uid,))
            conn.commit()
    return json_ok({"message": "Tudo zerado para o seu usuário."})


# ----------------------------
# WhatsApp Webhook
# ----------------------------
def extract_wa_message(payload: dict):
    """
    Tenta extrair (from_number, wa_name, text_body) do webhook.
    Suporta formato Cloud API (entry/changes/value/messages) e formatos simples.
    """
    from_number = None
    wa_name = ""
    text_body = ""

    # Cloud API padrão
    try:
        entry = (payload.get("entry") or [])[0]
        change = (entry.get("changes") or [])[0]
        value = change.get("value") or {}
        msgs = value.get("messages") or []
        if msgs:
            msg = msgs[0]
            from_number = msg.get("from")
            text = msg.get("text") or {}
            text_body = text.get("body") or ""

        contacts = value.get("contacts") or []
        if contacts:
            profile = (contacts[0].get("profile") or {})
            wa_name = profile.get("name") or ""
    except Exception:
        pass

    # Formato alternativo simples
    if not from_number:
        from_number = payload.get("from") or payload.get("wa_number")
        text_body = payload.get("text") or payload.get("body") or text_body
        wa_name = payload.get("name") or wa_name

    from_number = normalize_phone(from_number or "")
    text_body = (text_body or "").strip()

    return from_number, wa_name, text_body


def find_user_id_for_whatsapp(from_number: str, text_body: str) -> int | None:
    """
    Regra:
      1) Se texto tiver e-mail -> user por email
      2) Senão -> tenta por telefone (fim do telefone cadastrado)
    """
    email = looks_like_email(text_body)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if email:
                cur.execute("SELECT id FROM users WHERE email=%s", (email,))
                r = cur.fetchone()
                if r:
                    return int(r["id"])

            if from_number:
                # tenta match por telefone (normalizado). Alguns salvam com DDI/DDD, então compara por sufixo.
                cur.execute("SELECT id, telefone FROM users WHERE telefone <> ''")
                users = cur.fetchall()
                for u in users:
                    tel = normalize_phone(u["telefone"])
                    if tel and (from_number.endswith(tel) or tel.endswith(from_number)):
                        return int(u["id"])

    return None


@app.get("/webhooks/whatsapp")
def wa_verify():
    """
    Verificação do webhook (Meta).
    Configure as envs:
      WA_VERIFY_TOKEN (o mesmo token configurado no painel da Meta)
    """
    verify_token = os.environ.get("WA_VERIFY_TOKEN", "")
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token and token == verify_token:
        return challenge or "", 200

    return "forbidden", 403


@app.post("/webhooks/whatsapp")
def wa_webhook():
    payload = request.get_json(silent=True) or {}
    from_number, wa_name, text_body = extract_wa_message(payload)

    # IMPORTANTÍSSIMO: só grava se achar user_id
    user_id = find_user_id_for_whatsapp(from_number, text_body)

    if not user_id:
        # retorna 200 para não gerar retry infinito no webhook
        app.logger.warning(
            "WA webhook recebido mas sem user_id. from=%s text=%s",
            from_number, (text_body[:80] + "..." if len(text_body) > 80 else text_body)
        )
        return jsonify({"ok": True, "linked": False}), 200

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO wa_links (user_id, wa_number, wa_name, last_message_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (wa_number) DO UPDATE
                      SET user_id = EXCLUDED.user_id,
                          wa_name = EXCLUDED.wa_name,
                          last_message_at = NOW()
                    """,
                    (user_id, from_number, wa_name or ""),
                )
                conn.commit()

        return jsonify({"ok": True, "linked": True, "user_id": user_id}), 200
    except Exception as e:
        # ainda assim retorna 200 para evitar retry
        app.logger.exception("Erro ao salvar wa_links: %s", e)
        return jsonify({"ok": True, "linked": False}), 200


# ----------------------------
# Run
# ----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
