import os
import re
import json
import requests
from datetime import datetime, date

from flask import Flask, request, jsonify, session, render_template, send_from_directory

import bcrypt
import gspread
from google.oauth2.service_account import Credentials

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Text, ForeignKey, text, inspect
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.exc import IntegrityError

# =========================
# Flask
# =========================
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "financeai-secret-change-me")
app.config["JSON_AS_ASCII"] = False
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
# Se quiser forçar secure (somente HTTPS), descomente:
# app.config["SESSION_COOKIE_SECURE"] = True


@app.after_request
def headers_fix(response):
    mt = (response.mimetype or "").lower()
    if mt in ("text/html", "text/plain", "text/css", "application/javascript", "text/javascript"):
        response.headers["Content-Type"] = f"{mt}; charset=utf-8"
    elif mt == "application/json":
        response.headers["Content-Type"] = "application/json; charset=utf-8"
    elif mt.startswith("text/"):
        response.headers["Content-Type"] = f"{mt}; charset=utf-8"

    if mt == "text/html":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/robots.txt")
def robots_txt():
    return send_from_directory("static", "robots.txt")


# =========================
# ENV
# =========================
DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or "sqlite:///finance_ai.db"

WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v20.0")

# Sheets (backup/export)
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()
SHEETS_BACKUP_MODE = os.getenv("SHEETS_BACKUP_MODE", "manual").strip().lower()  # auto/manual/off

DEBUG_LOG_PAYLOAD = os.getenv("DEBUG_LOG_PAYLOAD", "0") == "1"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# =========================
# DB (Postgres/SQLite)
# =========================
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    nome_apelido = Column(String(80), nullable=True)
    nome_completo = Column(String(255), nullable=True)
    telefone = Column(String(40), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    transactions = relationship("Transaction", back_populates="user")


class WaLink(Base):
    __tablename__ = "wa_links"
    id = Column(Integer, primary_key=True)
    wa_number = Column(String(40), unique=True, nullable=False)  # e164 sem +
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    data = Column(String(10), nullable=False)      # YYYY-MM-DD
    tipo = Column(String(16), nullable=False)      # GASTO/RECEITA
    categoria = Column(String(64), nullable=False)
    descricao = Column(Text, nullable=True)
    valor = Column(String(32), nullable=False)     # "55.00"
    origem = Column(String(16), nullable=False, default="APP")  # APP/WA/CRON
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="transactions")


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"
    id = Column(Integer, primary_key=True)
    msg_id = Column(String(128), nullable=False, unique=True)
    wa_from = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def ensure_db_schema():
    """
    Corrige o seu erro atual:
      psycopg2.errors.UndefinedColumn: column "user_id" of relation "transactions" does not exist
    Porque Base.metadata.create_all NÃO altera tabelas antigas.
    Aqui a gente faz o mínimo necessário sem apagar dados.
    """
    Base.metadata.create_all(engine)

    insp = inspect(engine)

    # Se a tabela transactions existe, mas está antiga:
    if "transactions" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("transactions")}

        # se não existe user_id -> adiciona (nullable temporário), depois tenta preencher
        # (se você tinha user_email no schema antigo, ele tenta mapear)
        if "user_id" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE transactions ADD COLUMN user_id INTEGER"))
                # tenta preencher se existir user_email no schema antigo
                cols2 = {c["name"] for c in insp.get_columns("transactions")}
                if "user_email" in cols2:
                    # cria users faltando e mapeia
                    # 1) inserir usuários que ainda não existem
                    conn.execute(text("""
                        INSERT INTO users (email, password_hash, created_at)
                        SELECT DISTINCT t.user_email, :ph, :dt
                        FROM transactions t
                        WHERE t.user_email IS NOT NULL
                        AND NOT EXISTS (SELECT 1 FROM users u WHERE u.email = t.user_email)
                    """), {"ph": bcrypt.hashpw(b"temp123", bcrypt.gensalt()).decode("utf-8"),
                           "dt": datetime.utcnow()})
                    # 2) atualizar user_id
                    conn.execute(text("""
                        UPDATE transactions t
                        SET user_id = u.id
                        FROM users u
                        WHERE t.user_email = u.email
                        AND t.user_id IS NULL
                    """))

                # deixa NOT NULL se já conseguiu preencher tudo
                # (se ainda tiver null, não força pra não quebrar)
                null_count = conn.execute(text("SELECT COUNT(*) FROM transactions WHERE user_id IS NULL")).scalar() or 0
                if null_count == 0:
                    conn.execute(text("ALTER TABLE transactions ALTER COLUMN user_id SET NOT NULL"))

        # garante coluna origem
        if "origem" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE transactions ADD COLUMN origem VARCHAR(16) DEFAULT 'APP'"))
                conn.execute(text("UPDATE transactions SET origem='APP' WHERE origem IS NULL"))

    # wa_links
    if "wa_links" not in insp.get_table_names():
        Base.metadata.create_all(engine)


ensure_db_schema()


# =========================
# Sheets client (cache) - opcional
# =========================
_gs_client = None
_gs_spreadsheet = None
_gs_ws_lanc = None


def _gs_enabled() -> bool:
    return bool(SPREADSHEET_ID and SERVICE_ACCOUNT_JSON) and SHEETS_BACKUP_MODE in ("auto", "manual")


def gs_init_if_possible():
    """Inicializa Sheets se as env vars existirem. Nunca quebra o app se der erro."""
    global _gs_client, _gs_spreadsheet, _gs_ws_lanc

    if not _gs_enabled():
        return False

    if _gs_ws_lanc is not None:
        return True

    try:
        info = json.loads(SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        _gs_client = gspread.authorize(creds)
        _gs_spreadsheet = _gs_client.open_by_key(SPREADSHEET_ID)

        try:
            _gs_ws_lanc = _gs_spreadsheet.worksheet("Lancamentos")
        except Exception:
            _gs_ws_lanc = _gs_spreadsheet.add_worksheet(title="Lancamentos", rows=3000, cols=12)
            _gs_ws_lanc.append_row(
                ["user_email", "data", "tipo", "categoria", "descricao", "valor", "origem", "criado_em"],
                value_input_option="USER_ENTERED"
            )
        return True
    except Exception as e:
        app.logger.warning("Sheets init falhou: %s", str(e))
        return False


def sheets_append_rows(rows):
    """
    rows: list[list]
    Nunca derruba o app. Se falhar, loga.
    """
    if SHEETS_BACKUP_MODE != "auto":
        return
    if not gs_init_if_possible():
        return
    try:
        _gs_ws_lanc.append_rows(rows, value_input_option="USER_ENTERED")
    except Exception as e:
        app.logger.warning("Sheets append falhou: %s", str(e))


def sheets_export_month(user_email: str, tx_items: list[dict]):
    if not gs_init_if_possible():
        raise RuntimeError("Sheets não configurado (SPREADSHEET_ID/SERVICE_ACCOUNT_JSON) ou modo off.")

    created_at = datetime.utcnow().isoformat()
    rows = []
    for it in tx_items:
        rows.append([
            user_email,
            it["data"],
            it["tipo"],
            it["categoria"],
            it.get("descricao", "") or "",
            it["valor"],
            it.get("origem", "APP"),
            created_at
        ])

    if not rows:
        return 0

    _gs_ws_lanc.append_rows(rows, value_input_option="USER_ENTERED")
    return len(rows)


# =========================
# Helpers auth
# =========================
def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def current_user_id():
    return session.get("uid")


def require_login():
    return current_user_id()


# =========================
# Helpers gerais
# =========================
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_wa_number(raw) -> str:
    s = str(raw or "").strip().replace("+", "")
    s = re.sub(r"[^0-9]", "", s)
    return s


def parse_money_to_float(v) -> float:
    s = str(v or "").strip()
    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    s = re.sub(r"[^0-9\.\-]", "", s)
    try:
        return float(s)
    except:
        return 0.0


# =========================
# WhatsApp send
# =========================
def wa_send_text(to_number: str, textmsg: str):
    if not (WA_PHONE_NUMBER_ID and WA_ACCESS_TOKEN):
        return
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": normalize_wa_number(to_number),
        "type": "text",
        "text": {"body": str(textmsg or "")},
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code >= 400:
            app.logger.warning("WA send error %s %s", r.status_code, r.text)
    except Exception as e:
        app.logger.warning("WA send exception %s", str(e))


def extract_messages(payload: dict):
    """
    Retorna lista de dict:
      {id, from, type, text_body, media_id, caption}
    """
    out = []
    try:
        for e in payload.get("entry", []) or []:
            for ch in e.get("changes", []) or []:
                v = ch.get("value", {}) or {}
                for m in (v.get("messages", []) or []):
                    msg_id = m.get("id")
                    wa_from = m.get("from")
                    mtype = m.get("type")
                    if not msg_id or not wa_from or not mtype:
                        continue

                    item = {"id": msg_id, "from": wa_from, "type": mtype}
                    if mtype == "text":
                        item["text_body"] = (((m.get("text") or {}) or {}).get("body", "") or "")
                    elif mtype in ("image", "document", "audio", "video"):
                        media = m.get(mtype, {}) or {}
                        item["media_id"] = media.get("id")
                        item["caption"] = (media.get("caption") or "") or ""
                    out.append(item)
    except Exception:
        return []
    return out


VALUE_RE = re.compile(r"^([+\-])?\s*(?:R\$\s*)?(\d+(?:[.,]\d{1,2})?)\s*(.*)$", re.IGNORECASE)


def parse_finance_line(line: str):
    """
    Aceita:
      + 35,90 mercado
      - 120 aluguel
      55 mercado  (assume gasto)
      receita 2500 salario
      gasto 32,90 mercado
    """
    raw = (line or "").strip()
    if not raw:
        return None

    low = raw.lower()

    # prefixo textual
    if low.startswith(("receita ", "entrada ")):
        tipo = "RECEITA"
        raw2 = raw.split(" ", 1)[1].strip()
    elif low.startswith(("gasto ", "despesa ")):
        tipo = "GASTO"
        raw2 = raw.split(" ", 1)[1].strip()
    else:
        tipo = None
        raw2 = raw

    m = VALUE_RE.match(raw2)
    if not m:
        return None

    sign = m.group(1)
    val = m.group(2)
    rest = (m.group(3) or "").strip()

    if sign == "+":
        tipo2 = "RECEITA"
    elif sign == "-":
        tipo2 = "GASTO"
    else:
        tipo2 = tipo or "GASTO"

    valor = parse_money_to_float(val)

    # categoria = 1a palavra, descricao = resto
    if rest:
        parts = rest.split(" ", 1)
        categoria = parts[0].strip().title() if parts[0].strip() else "Geral"
        descricao = parts[1].strip() if len(parts) > 1 else ""
    else:
        categoria = "Geral"
        descricao = ""

    return {
        "tipo": tipo2,
        "valor": f"{valor:.2f}",
        "categoria": categoria,
        "descricao": descricao,
        "data": date.today().isoformat()
    }


# =========================
# Routes base
# =========================
@app.get("/health")
def health():
    return jsonify({"ok": True, "sheets_mode": SHEETS_BACKUP_MODE})


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/debug/sheets")
def debug_sheets():
    return jsonify({
        "SPREADSHEET_ID_set": bool(SPREADSHEET_ID),
        "SERVICE_ACCOUNT_JSON_set": bool(SERVICE_ACCOUNT_JSON),
        "SHEETS_BACKUP_MODE": SHEETS_BACKUP_MODE,
        "enabled": _gs_enabled(),
        "ready": bool(_gs_ws_lanc is not None),
    })


# =========================
# Auth API (bcrypt)
# =========================
@app.post("/api/register")
def api_register():
    data = request.get_json(force=True) or {}
    email = str(data.get("email", "")).lower().strip()
    senha = str(data.get("senha", ""))
    confirmar = str(data.get("confirmar_senha", ""))

    nome_apelido = str(data.get("nome_apelido", "") or "")
    nome_completo = str(data.get("nome_completo", "") or "")
    telefone = str(data.get("telefone", "") or "")

    if not email or not senha:
        return jsonify(error="Email e senha obrigatórios"), 400
    if not EMAIL_RE.match(email):
        return jsonify(error="Email inválido"), 400
    if senha != confirmar:
        return jsonify(error="Senhas não conferem"), 400
    if len(senha) < 6:
        return jsonify(error="Senha deve ter pelo menos 6 caracteres"), 400

    db = SessionLocal()
    try:
        u = User(
            email=email,
            password_hash=hash_password(senha),
            nome_apelido=nome_apelido,
            nome_completo=nome_completo,
            telefone=telefone,
        )
        db.add(u)
        db.commit()
        session["uid"] = u.id
        session["email"] = u.email
        return jsonify(email=u.email)
    except IntegrityError:
        db.rollback()
        return jsonify(error="Email já cadastrado"), 400
    finally:
        db.close()


@app.post("/api/login")
def api_login():
    data = request.get_json(force=True) or {}
    email = str(data.get("email", "")).lower().strip()
    senha = str(data.get("senha", ""))

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).first()
        if not u or not verify_password(senha, u.password_hash):
            return jsonify(error="Email ou senha inválidos"), 401
        session["uid"] = u.id
        session["email"] = u.email
        return jsonify(email=u.email)
    finally:
        db.close()


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/reset_password")
def api_reset_password():
    data = request.get_json(force=True) or {}
    email = str(data.get("email", "")).lower().strip()
    nova = str(data.get("nova_senha", ""))
    conf = str(data.get("confirmar", ""))

    if not email or not nova:
        return jsonify(error="Email e nova senha obrigatórios"), 400
    if not EMAIL_RE.match(email):
        return jsonify(error="Email inválido"), 400
    if nova != conf:
        return jsonify(error="Senhas não conferem"), 400
    if len(nova) < 6:
        return jsonify(error="Senha deve ter pelo menos 6 caracteres"), 400

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).first()
        if not u:
            return jsonify(error="Email não encontrado"), 404
        u.password_hash = hash_password(nova)
        db.commit()
        return jsonify(ok=True)
    finally:
        db.close()


# =========================
# Lancamentos API (Postgres)
# =========================
@app.get("/api/lancamentos")
def api_list_lancamentos():
    uid = require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    limit = int(request.args.get("limit", 50))
    db = SessionLocal()
    try:
        q = (
            db.query(Transaction)
            .filter(Transaction.user_id == uid)
            .order_by(Transaction.data.desc(), Transaction.id.desc())
        )
        items = []
        for t in q.limit(limit).all():
            items.append({
                "row": t.id,
                "user_email": session.get("email", ""),
                "data": t.data,
                "tipo": t.tipo,
                "categoria": t.categoria,
                "descricao": t.descricao or "",
                "valor": t.valor,
                "origem": t.origem,
                "criado_em": t.created_at.isoformat() if t.created_at else ""
            })
        return jsonify(items=items)
    finally:
        db.close()


@app.post("/api/lancamentos")
def api_create_lancamento():
    uid = require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    payload = request.get_json(force=True) or {}
    db = SessionLocal()
    try:
        t = Transaction(
            user_id=uid,
            data=str(payload.get("data") or date.today().isoformat()),
            tipo=str(payload.get("tipo") or "GASTO").upper(),
            categoria=str(payload.get("categoria") or "Geral").strip().title(),
            descricao=str(payload.get("descricao") or ""),
            valor=f"{parse_money_to_float(payload.get('valor')):.2f}",
            origem="APP",
        )
        db.add(t)
        db.commit()

        # backup automático no Sheets (se mode=auto) sem derrubar o app
        sheets_append_rows([[
            session.get("email", ""),
            t.data, t.tipo, t.categoria, t.descricao or "", t.valor, t.origem,
            datetime.utcnow().isoformat()
        ]])

        return jsonify(ok=True, id=t.id)
    except Exception as e:
        db.rollback()
        app.logger.exception("Erro ao criar lançamento: %s", str(e))
        return jsonify(error="Erro interno ao salvar lançamento"), 500
    finally:
        db.close()


@app.put("/api/lancamentos/<int:row_id>")
def api_edit_lancamento(row_id: int):
    uid = require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    payload = request.get_json(force=True) or {}
    db = SessionLocal()
    try:
        t = db.query(Transaction).filter(Transaction.id == row_id, Transaction.user_id == uid).first()
        if not t:
            return jsonify(error="Sem permissão ou inexistente"), 403

        t.data = str(payload.get("data") or t.data)
        t.tipo = str(payload.get("tipo") or t.tipo).upper()
        t.categoria = str(payload.get("categoria") or t.categoria).strip().title()
        t.descricao = str(payload.get("descricao") or "")
        t.valor = f"{parse_money_to_float(payload.get('valor')):.2f}"
        db.commit()
        return jsonify(ok=True)
    finally:
        db.close()


@app.delete("/api/lancamentos/<int:row_id>")
def api_delete_lancamento(row_id: int):
    uid = require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    db = SessionLocal()
    try:
        t = db.query(Transaction).filter(Transaction.id == row_id, Transaction.user_id == uid).first()
        if not t:
            return jsonify(error="Sem permissão ou inexistente"), 403
        db.delete(t)
        db.commit()
        return jsonify(ok=True)
    finally:
        db.close()


@app.get("/api/dashboard")
def api_dashboard():
    uid = require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    mes = int(request.args.get("mes"))
    ano = int(request.args.get("ano"))

    db = SessionLocal()
    try:
        receitas = 0.0
        gastos = 0.0
        rows = db.query(Transaction).filter(Transaction.user_id == uid).all()
        for r in rows:
            try:
                d = datetime.fromisoformat(r.data)
            except:
                continue
            if d.month == mes and d.year == ano:
                v = parse_money_to_float(r.valor)
                if str(r.tipo).upper() == "RECEITA":
                    receitas += v
                elif str(r.tipo).upper() == "GASTO":
                    gastos += v
        return jsonify(receitas=receitas, gastos=gastos, saldo=receitas - gastos)
    finally:
        db.close()


# =========================
# Exportar mês -> Sheets
# =========================
@app.post("/api/export-month")
def api_export_month():
    uid = require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    if SHEETS_BACKUP_MODE == "off":
        return jsonify(error="Sheets está desligado (SHEETS_BACKUP_MODE=off)."), 400

    data = request.get_json(force=True) or {}
    mes = int(data.get("mes"))
    ano = int(data.get("ano"))

    db = SessionLocal()
    try:
        q = db.query(Transaction).filter(Transaction.user_id == uid).all()
        items = []
        for t in q:
            try:
                d = datetime.fromisoformat(t.data)
            except:
                continue
            if d.month == mes and d.year == ano:
                items.append({
                    "data": t.data,
                    "tipo": t.tipo,
                    "categoria": t.categoria,
                    "descricao": t.descricao or "",
                    "valor": t.valor,
                    "origem": t.origem
                })

        n = sheets_export_month(session.get("email", ""), items)
        return jsonify(ok=True, exported=n)
    finally:
        db.close()


# =========================
# WhatsApp link helpers
# =========================
def db_find_user_by_email(db, email: str):
    return db.query(User).filter(User.email == (email or "").lower().strip()).first()


def db_find_email_by_wa(db, wa_number: str):
    wa = normalize_wa_number(wa_number)
    link = db.query(WaLink).filter(WaLink.wa_number == wa).first()
    if not link:
        return None
    u = db.query(User).filter(User.id == link.user_id).first()
    return u.email if u else None


def db_link_wa_to_email(db, wa_number: str, email: str):
    wa = normalize_wa_number(wa_number)
    email = (email or "").lower().strip()
    if not wa or not email or not EMAIL_RE.match(email):
        return False, "Número e email válidos são obrigatórios."

    u = db_find_user_by_email(db, email)
    if not u:
        return False, "Email não encontrado no app. Crie a conta primeiro."

    existing = db.query(WaLink).filter(WaLink.wa_number == wa).first()
    if existing:
        existing.user_id = u.id
        db.commit()
        return True, f"✅ WhatsApp atualizado para {email}."

    db.add(WaLink(wa_number=wa, user_id=u.id))
    db.commit()
    return True, f"✅ WhatsApp conectado ao email {email}."


def db_unlink_wa(db, wa_number: str):
    wa = normalize_wa_number(wa_number)
    link = db.query(WaLink).filter(WaLink.wa_number == wa).first()
    if not link:
        return False, "Esse número não estava conectado."
    db.delete(link)
    db.commit()
    return True, "✅ Número desconectado."


def db_is_processed(db, msg_id: str):
    return db.query(ProcessedMessage).filter(ProcessedMessage.msg_id == msg_id).first() is not None


def db_mark_processed(db, msg_id: str, wa_from: str):
    db.add(ProcessedMessage(msg_id=msg_id, wa_from=normalize_wa_number(wa_from)))
    db.commit()


# =========================
# WhatsApp webhook
# =========================
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
    if DEBUG_LOG_PAYLOAD:
        app.logger.info("WA payload: %s", json.dumps(payload)[:4000])

    db = SessionLocal()
    try:
        msgs = extract_messages(payload)

        for m in msgs:
            msg_id = m.get("id")
            wa_from = m.get("from")
            mtype = m.get("type")

            if not msg_id or not wa_from or not mtype:
                continue

            # dedupe
            if db_is_processed(db, msg_id):
                continue

            # marca como processada cedo (evita dupla em caso de retry)
            db_mark_processed(db, msg_id, wa_from)

            from_number = normalize_wa_number(wa_from)

            if mtype == "text":
                body = str(m.get("text_body") or "").strip()
                low = body.lower()

                # comandos
                if low.startswith("conectar "):
                    email = body.split(" ", 1)[1].strip()
                    ok, resp = db_link_wa_to_email(db, from_number, email)
                    wa_send_text(from_number, resp + ("\n\nAgora envie: gasto 32,90 mercado" if ok else ""))
                    continue

                if low in ("desconectar", "desconectar whatsapp"):
                    _, resp = db_unlink_wa(db, from_number)
                    wa_send_text(from_number, resp)
                    continue

                # ✅ fallback: se o usuário mandar SÓ o email, conecta também
                if EMAIL_RE.match(body):
                    ok, resp = db_link_wa_to_email(db, from_number, body)
                    wa_send_text(from_number, resp + ("\n\nAgora envie: gasto 32,90 mercado" if ok else ""))
                    continue

                # precisa estar linkado
                user_email = db_find_email_by_wa(db, from_number)
                if not user_email:
                    wa_send_text(from_number,
                        "🔒 Seu WhatsApp ainda não está conectado.\n\n"
                        "Envie:\n"
                        "• conectar SEU_EMAIL_DO_APP\n"
                        "ou mande apenas seu email (ex: david@email.com)."
                    )
                    continue

                parsed = parse_finance_line(body)
                if not parsed:
                    wa_send_text(from_number,
                        "Não entendi 😅\n\nUse assim:\n"
                        "• gasto 32,90 mercado\n"
                        "• receita 2500 salario\n"
                        "• + 35,90 mercado\n"
                        "• - 120 aluguel\n"
                        "• 32,90 mercado (assume gasto)"
                    )
                    continue

                u = db_find_user_by_email(db, user_email)
                if not u:
                    wa_send_text(from_number, "Seu email não existe no app. Crie sua conta primeiro.")
                    continue

                t = Transaction(
                    user_id=u.id,
                    data=parsed["data"],
                    tipo=parsed["tipo"],
                    categoria=parsed["categoria"],
                    descricao=parsed["descricao"],
                    valor=parsed["valor"],
                    origem="WA",
                )
                db.add(t)
                db.commit()

                # backup auto
                sheets_append_rows([[
                    user_email,
                    t.data, t.tipo, t.categoria, t.descricao or "", t.valor, t.origem,
                    datetime.utcnow().isoformat()
                ]])

                wa_send_text(from_number,
                    "✅ Lançamento salvo!\n"
                    f"Tipo: {t.tipo}\n"
                    f"Valor: R$ {t.valor.replace('.', ',')}\n"
                    f"Categoria: {t.categoria}\n"
                    f"Data: {t.data}"
                )
                continue

            # mídias: salva placeholder
            if mtype in ("image", "document", "audio", "video"):
                user_email = db_find_email_by_wa(db, from_number)
                if not user_email:
                    wa_send_text(from_number, "🔒 Conecte primeiro: conectar SEU_EMAIL_DO_APP (ou mande seu email)")
                    continue

                u = db_find_user_by_email(db, user_email)
                if not u:
                    wa_send_text(from_number, "Seu email não existe no app. Crie sua conta primeiro.")
                    continue

                media_id = m.get("media_id")
                caption = str(m.get("caption") or "").strip()

                t = Transaction(
                    user_id=u.id,
                    data=date.today().isoformat(),
                    tipo="GASTO",
                    categoria="Comprovante",
                    descricao=f"{caption} [MID:{media_id}]".strip(),
                    valor="0.00",
                    origem="WA",
                )
                db.add(t)
                db.commit()

                sheets_append_rows([[
                    user_email,
                    t.data, t.tipo, t.categoria, t.descricao or "", t.valor, t.origem,
                    datetime.utcnow().isoformat()
                ]])

                wa_send_text(from_number,
                    "📎 Comprovante recebido!\n"
                    "Salvei como 'Comprovante' (valor 0,00) para você editar depois no app."
                )
                continue

        return "ok", 200

    except Exception as e:
        app.logger.exception("WA webhook error: %s", str(e))
        return "ok", 200
    finally:
        db.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
