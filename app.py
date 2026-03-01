import os
import re
import json
import requests
from datetime import datetime, date, timezone, timedelta

from flask import Flask, request, jsonify, session, render_template, send_from_directory

import bcrypt
import gspread
from google.oauth2.service_account import Credentials

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Text, ForeignKey
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
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///finance_ai.db"  # fallback dev

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
# DB (Postgres)
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
Base.metadata.create_all(engine)


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
        except gspread.exceptions.WorksheetNotFound:
            _gs_ws_lanc = _gs_spreadsheet.add_worksheet(title="Lancamentos", rows=3000, cols=10)
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


def sheets_export_month(user_email: str, mes: int, ano: int, tx_items: list[dict]):
    """
    Exporta todos os lançamentos do mês para aba "Lancamentos" (append).
    Não apaga nada, só adiciona linhas.
    """
    if not gs_init_if_possible():
        raise RuntimeError("Sheets não configurado (SPREADSHEET_ID/SERVICE_ACCOUNT_JSON) ou modo off.")

    # Marca de export
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
def normalize_wa_number(raw) -> str:
    s = str(raw or "").strip().replace("+", "")
    s = re.sub(r"[^0-9]", "", s)
    return s


def parse_money_to_float(v: str) -> float:
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
def wa_send_text(to_number: str, text: str):
    if not (WA_PHONE_NUMBER_ID and WA_ACCESS_TOKEN):
        return
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": normalize_wa_number(to_number),
        "type": "text",
        "text": {"body": str(text or "")},
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code >= 400:
            app.logger.warning("WA send error %s %s", r.status_code, r.text)
    except Exception as e:
        app.logger.warning("WA send exception %s", str(e))


def extract_text_messages(payload: dict):
    out = []
    try:
        for e in payload.get("entry", []) or []:
            for ch in e.get("changes", []) or []:
                v = ch.get("value", {}) or {}
                for m in (v.get("messages", []) or []):
                    if (m.get("type") == "text"):
                        msg_id = m.get("id")
                        wa_from = m.get("from")
                        body = ((m.get("text") or {}) or {}).get("body", "")
                        if msg_id and wa_from and body:
                            out.append((msg_id, wa_from, body))
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

        # (1) backup automático no Sheets (se mode=auto) sem derrubar o app
        sheets_append_rows([[
            session.get("email", ""),
            t.data,
            t.tipo,
            t.categoria,
            t.descricao or "",
            t.valor,
            t.origem,
            datetime.utcnow().isoformat()
        ]])

        return jsonify(ok=True, id=t.id)
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
# (2) Exportar mês -> Sheets (botão no dashboard)
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
    user_email = session.get("email", "")

    db = SessionLocal()
    try:
        txs = db.query(Transaction).filter(Transaction.user_id == uid).all()
        items = []
        for t in txs:
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

        count = sheets_export_month(user_email, mes, ano, items)
        return jsonify(ok=True, exported=count)
    finally:
        db.close()


# =========================
# WhatsApp Webhook (comandos pro + (1) auto backup + (3) resumo)
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
        app.logger.info("WA payload: %s", json.dumps(payload)[:3000])

    msgs = extract_text_messages(payload)
    if not msgs:
        return "ok", 200

    db = SessionLocal()
    try:
        for msg_id, wa_from, body in msgs:
            wa_from_n = normalize_wa_number(wa_from)
            text = str(body or "").strip()
            low = text.lower()

            # Dedup definitivo
            try:
                db.add(ProcessedMessage(msg_id=msg_id, wa_from=wa_from_n))
                db.commit()
            except IntegrityError:
                db.rollback()
                continue

            # conectar email
            if low.startswith("conectar "):
                email = text.split(" ", 1)[1].strip().lower()
                u = db.query(User).filter(User.email == email).first()
                if not u:
                    wa_send_text(wa_from_n, "❌ Email não encontrado no app. Crie a conta no site primeiro.")
                    continue

                link = db.query(WaLink).filter(WaLink.wa_number == wa_from_n).first()
                if link:
                    link.user_id = u.id
                else:
                    db.add(WaLink(wa_number=wa_from_n, user_id=u.id))
                db.commit()

                wa_send_text(wa_from_n, "✅ Conectado! Agora envie:\n+ 35,90 mercado\n- 120 aluguel\nlistar 10\nresumo mes")
                continue

            # desconectar
            if low in ("desconectar", "desconectar whatsapp"):
                link = db.query(WaLink).filter(WaLink.wa_number == wa_from_n).first()
                if link:
                    db.delete(link)
                    db.commit()
                    wa_send_text(wa_from_n, "✅ WhatsApp desconectado.")
                else:
                    wa_send_text(wa_from_n, "Esse número não estava conectado.")
                continue

            # precisa estar conectado
            link = db.query(WaLink).filter(WaLink.wa_number == wa_from_n).first()
            if not link:
                wa_send_text(
                    wa_from_n,
                    "🔒 Seu WhatsApp ainda não está conectado.\n\nEnvie:\nconectar SEU_EMAIL_DO_APP\nEx: conectar david@email.com"
                )
                continue

            uid = link.user_id
            user = db.query(User).filter(User.id == uid).first()
            user_email = user.email if user else ""

            # listar N
            m_list = re.match(r"^listar\s+(\d+)$", low)
            if m_list:
                n = min(int(m_list.group(1)), 30)
                txs = (
                    db.query(Transaction)
                    .filter(Transaction.user_id == uid)
                    .order_by(Transaction.data.desc(), Transaction.id.desc())
                    .limit(n)
                    .all()
                )
                if not txs:
                    wa_send_text(wa_from_n, "Nenhum lançamento ainda.")
                else:
                    lines = []
                    for t in txs:
                        lines.append(f"{t.id}) {t.data} • {t.tipo} • R$ {t.valor} • {t.categoria}")
                    wa_send_text(wa_from_n, "🧾 Últimos lançamentos:\n" + "\n".join(lines))
                continue

            # apagar ID
            m_del = re.match(r"^apagar\s+(\d+)$", low)
            if m_del:
                tid = int(m_del.group(1))
                t = db.query(Transaction).filter(Transaction.id == tid, Transaction.user_id == uid).first()
                if not t:
                    wa_send_text(wa_from_n, "❌ ID não encontrado.")
                else:
                    db.delete(t)
                    db.commit()
                    wa_send_text(wa_from_n, f"✅ Apagado (ID {tid}).")
                continue

            # editar ID <linha>
            m_edit = re.match(r"^editar\s+(\d+)\s+(.+)$", text, flags=re.IGNORECASE)
            if m_edit:
                tid = int(m_edit.group(1))
                rest_line = m_edit.group(2).strip()
                parsed = parse_finance_line(rest_line)
                if not parsed:
                    wa_send_text(wa_from_n, "Não entendi a edição. Ex: editar 12 - 45,00 mercado")
                    continue

                t = db.query(Transaction).filter(Transaction.id == tid, Transaction.user_id == uid).first()
                if not t:
                    wa_send_text(wa_from_n, "❌ ID não encontrado.")
                    continue

                t.tipo = parsed["tipo"]
                t.valor = parsed["valor"]
                t.categoria = parsed["categoria"]
                t.descricao = parsed["descricao"]
                t.data = parsed["data"]
                db.commit()

                wa_send_text(wa_from_n, f"✅ Editado (ID {tid}).")
                continue

            # (3) resumo mes
            if low in ("resumo mes", "resumo mês"):
                today = date.today()
                mes = today.month
                ano = today.year
                txs = db.query(Transaction).filter(Transaction.user_id == uid).all()
                receitas = 0.0
                gastos = 0.0
                for t in txs:
                    try:
                        d = datetime.fromisoformat(t.data)
                    except:
                        continue
                    if d.month == mes and d.year == ano:
                        v = parse_money_to_float(t.valor)
                        if t.tipo == "RECEITA":
                            receitas += v
                        elif t.tipo == "GASTO":
                            gastos += v
                saldo = receitas - gastos
                wa_send_text(wa_from_n, f"📊 Resumo do mês:\nReceitas: R$ {receitas:.2f}\nGastos: R$ {gastos:.2f}\nSaldo: R$ {saldo:.2f}")
                continue

            # padrão: lançar (uma ou várias linhas)
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            created = []
            created_rows_for_sheets = []

            for ln in lines:
                parsed = parse_finance_line(ln)
                if not parsed:
                    continue

                tx = Transaction(
                    user_id=uid,
                    data=parsed["data"],
                    tipo=parsed["tipo"],
                    categoria=parsed["categoria"],
                    descricao=parsed["descricao"],
                    valor=parsed["valor"],
                    origem="WA",
                )
                db.add(tx)
                db.flush()  # pega ID
                created.append(tx.id)

                # (1) auto backup (se mode=auto)
                created_rows_for_sheets.append([
                    user_email,
                    tx.data,
                    tx.tipo,
                    tx.categoria,
                    tx.descricao or "",
                    tx.valor,
                    tx.origem,
                    datetime.utcnow().isoformat()
                ])

            db.commit()

            if created_rows_for_sheets:
                sheets_append_rows(created_rows_for_sheets)

            if not created:
                wa_send_text(wa_from_n,
                    "Não entendi 😅\n\nUse:\n+ 35,90 mercado\n- 120 aluguel\nlistar 10\nresumo mes"
                )
            elif len(created) == 1:
                wa_send_text(wa_from_n, f"✅ Lançamento salvo! ID {created[0]}")
            else:
                wa_send_text(wa_from_n, f"✅ {len(created)} lançamentos salvos! IDs: {', '.join(map(str, created))}")

        return "ok", 200
    finally:
        db.close()


# =========================
# (3) Resumo automático mensal via WhatsApp (endpoint p/ cron)
# =========================
@app.post("/cron/send-monthly-summaries")
def cron_send_monthly_summaries():
    """
    Para usar com Railway Cron (ou qualquer cron externo).
    Envia resumo do mês atual para TODOS os wa_links.
    Segurança simples via token:
      defina CRON_TOKEN env e envie header: X-CRON-TOKEN
    """
    cron_token = os.getenv("CRON_TOKEN", "").strip()
    if cron_token:
        got = (request.headers.get("X-CRON-TOKEN", "") or "").strip()
        if got != cron_token:
            return jsonify(error="forbidden"), 403

    today = date.today()
    mes = today.month
    ano = today.year

    db = SessionLocal()
    try:
        links = db.query(WaLink).all()
        sent = 0

        for link in links:
            uid = link.user_id
            wa = link.wa_number

            txs = db.query(Transaction).filter(Transaction.user_id == uid).all()
            receitas = 0.0
            gastos = 0.0

            for t in txs:
                try:
                    d = datetime.fromisoformat(t.data)
                except:
                    continue
                if d.month == mes and d.year == ano:
                    v = parse_money_to_float(t.valor)
                    if t.tipo == "RECEITA":
                        receitas += v
                    elif t.tipo == "GASTO":
                        gastos += v

            saldo = receitas - gastos
            wa_send_text(wa, f"📊 Resumo do mês {mes:02d}/{ano}:\nReceitas: R$ {receitas:.2f}\nGastos: R$ {gastos:.2f}\nSaldo: R$ {saldo:.2f}")
            sent += 1

        return jsonify(ok=True, sent=sent)
    finally:
        db.close()


# =========================
# Main
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
