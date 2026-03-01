import os
import re
import json
import time
import datetime as dt
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask, request, jsonify

import gspread
from google.oauth2.service_account import Credentials

from sqlalchemy import (
    create_engine, Column, String, DateTime, Integer, Text, Boolean
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import IntegrityError


# =========================
# Config
# =========================
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v20.0")

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "")

DEBUG_LOG_PAYLOAD = os.getenv("DEBUG_LOG_PAYLOAD", "0") == "1"

# Use variável de referência no Railway:
# DATABASE_URL = ${Postgres.DATABASE_URL}
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///finance_ai.db"  # fallback local

# Token simples para endpoints internos (sync)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# =========================
# Flask
# =========================
app = Flask(__name__)

# =========================
# DB
# =========================
Base = declarative_base()

class ProcessedMessage(Base):
    __tablename__ = "processed_messages"
    id = Column(Integer, primary_key=True)
    msg_id = Column(String(128), nullable=False, unique=True)
    wa_from = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    wa_from = Column(String(64), nullable=True)
    date = Column(String(10), nullable=False)      # YYYY-MM-DD
    type = Column(String(16), nullable=False)      # GASTO/RECEITA
    value = Column(String(32), nullable=False)     # "55.00"
    category = Column(String(64), nullable=False)
    raw_text = Column(Text, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)

    # Controle de sync com Sheets (pra nunca travar o WA)
    sheets_synced = Column(Boolean, default=False, nullable=False)
    sheets_synced_at = Column(DateTime, nullable=True)
    sheets_error = Column(Text, nullable=True)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)

# =========================
# Google Sheets (somente APPEND)
# =========================
_gspread_client = None
_spreadsheet = None
_ws_lanc = None

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def _get_gspread_client():
    global _gspread_client
    if _gspread_client:
        return _gspread_client

    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError("SERVICE_ACCOUNT_JSON não definido")

    try:
        info = json.loads(SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as e:
        raise RuntimeError("SERVICE_ACCOUNT_JSON inválido (não é JSON).") from e

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    _gspread_client = gspread.authorize(creds)
    return _gspread_client

def _ensure_sheets_ready():
    """
    Abre/cria a aba 1x. Depois só append_rows.
    """
    global _spreadsheet, _ws_lanc

    if _ws_lanc is not None:
        return

    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID não definido")

    gc = _get_gspread_client()
    _spreadsheet = gc.open_by_key(SPREADSHEET_ID)

    try:
        _ws_lanc = _spreadsheet.worksheet("Lancamentos")
    except gspread.exceptions.WorksheetNotFound:
        _ws_lanc = _spreadsheet.add_worksheet(title="Lancamentos", rows=2000, cols=10)
        _ws_lanc.append_row(["wa_from", "date", "type", "value", "category", "raw_text", "created_at"])

def sheets_append_rows_with_retry(rows, max_retries=4):
    """
    Tenta append com retry (429/5xx).
    Se continuar falhando, levanta exceção (mas chamador NÃO derruba WA).
    """
    _ensure_sheets_ready()

    last_err = None
    for attempt in range(max_retries):
        try:
            _ws_lanc.append_rows(rows, value_input_option="USER_ENTERED")
            return
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            # Heurística simples: quota/429/temporário
            if ("429" in msg) or ("quota" in msg) or ("rate" in msg) or ("503" in msg) or ("500" in msg):
                sleep_s = 1.5 * (2 ** attempt)
                time.sleep(sleep_s)
                continue
            raise

    raise last_err

# =========================
# Util: parse de valor PT-BR
# =========================
_money_re = re.compile(r"[-+]?\d[\d\.]*[,\.]?\d*")

def parse_money_to_decimal(s: str) -> Decimal:
    s = s.strip()
    m = _money_re.search(s)
    if not m:
        raise ValueError("Sem número")
    num = m.group(0)

    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    else:
        if "," in num:
            num = num.replace(".", "").replace(",", ".")
        else:
            parts = num.split(".")
            if len(parts) > 2:
                num = "".join(parts[:-1]) + "." + parts[-1]

    try:
        return Decimal(num)
    except InvalidOperation as e:
        raise ValueError(f"Valor inválido: {s}") from e

def normalize_category(cat: str) -> str:
    cat = cat.strip().lower()
    cat = re.sub(r"\s+", " ", cat)
    return cat[:64] if cat else "geral"

RECEITA_HINTS = {
    "recebi", "receber", "receita", "salario", "salário",
    "pagamento", "pix recebido", "entrada", "ganhei"
}
GASTO_HINTS = {"gastei", "gasto", "despesa", "paguei", "saída", "saida"}

def guess_type(text: str, category: str) -> str:
    t = text.lower()
    c = category.lower()

    # salário SEMPRE receita
    if "salari" in t or "salari" in c:
        return "RECEITA"

    if any(h in t for h in RECEITA_HINTS):
        return "RECEITA"
    if any(h in t for h in GASTO_HINTS):
        return "GASTO"

    # padrão
    return "GASTO"

def parse_lines_to_transactions(wa_from: str, body_text: str, date_str: str):
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
    txs = []

    for ln in lines:
        value = parse_money_to_decimal(ln)

        cat = re.sub(_money_re, " ", ln)
        cat = re.sub(r"[^\wÀ-ÿ\s]", " ", cat, flags=re.UNICODE)
        cat = re.sub(r"\s+", " ", cat).strip()

        category = normalize_category(cat) if cat else "geral"
        tx_type = guess_type(ln, category)

        txs.append({
            "wa_from": wa_from,
            "date": date_str,
            "type": tx_type,
            "value": str(value.quantize(Decimal("0.01"))),
            "category": category,
            "raw_text": ln,
        })

    return txs

# =========================
# WhatsApp API
# =========================
def wa_send_text(to: str, text: str):
    if not (WA_ACCESS_TOKEN and WA_PHONE_NUMBER_ID):
        return

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        requests.post(url, headers=headers, json=payload, timeout=20)
    except Exception:
        pass

def extract_text_messages(payload: dict):
    out = []
    try:
        for e in payload.get("entry", []):
            for ch in e.get("changes", []):
                v = ch.get("value", {})
                for m in v.get("messages", []):
                    msg_id = m.get("id")
                    wa_from = m.get("from")
                    if m.get("type") == "text":
                        text = (m.get("text") or {}).get("body", "")
                        if msg_id and wa_from and text:
                            out.append((msg_id, wa_from, text))
    except Exception:
        return []
    return out

# =========================
# Routes
# =========================
@app.get("/health")
def health():
    return jsonify({"ok": True})

@app.get("/")
def root():
    return jsonify({"ok": True, "service": "FinanceAI"})

@app.get("/webhooks/whatsapp")
def whatsapp_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

@app.post("/webhooks/whatsapp")
def whatsapp_webhook():
    payload = request.get_json(silent=True) or {}
    if DEBUG_LOG_PAYLOAD:
        app.logger.info("INCOMING WA WEBHOOK: %s", json.dumps(payload)[:3000])

    msgs = extract_text_messages(payload)
    if not msgs:
        return "OK", 200

    today = dt.date.today().isoformat()

    db = SessionLocal()
    try:
        for msg_id, wa_from, text in msgs:
            # 1) Dedup definitivo
            try:
                db.add(ProcessedMessage(msg_id=msg_id, wa_from=wa_from))
                db.commit()
            except IntegrityError:
                db.rollback()
                continue

            # 2) Parse
            try:
                txs = parse_lines_to_transactions(wa_from, text, today)
            except Exception:
                wa_send_text(wa_from, "Não entendi. Ex: 55 mercado (ou várias linhas).")
                continue

            # 3) Salva transações no DB
            created_at_iso = dt.datetime.utcnow().isoformat()
            created_rows = []
            tx_ids = []

            for t in txs:
                tx_obj = Transaction(
                    wa_from=t["wa_from"],
                    date=t["date"],
                    type=t["type"],
                    value=t["value"],
                    category=t["category"],
                    raw_text=t["raw_text"],
                    sheets_synced=False,
                    sheets_error=None,
                )
                db.add(tx_obj)
                db.flush()  # pega id sem commit ainda
                tx_ids.append(tx_obj.id)

                created_rows.append([
                    t["wa_from"], t["date"], t["type"], t["value"],
                    t["category"], t["raw_text"], created_at_iso
                ])

            db.commit()

            # 4) Tenta mandar pro Sheets (NÃO derruba WA)
            try:
                sheets_append_rows_with_retry(created_rows, max_retries=4)
                # marcou synced
                now = dt.datetime.utcnow()
                for tid in tx_ids:
                    obj = db.get(Transaction, tid)
                    if obj:
                        obj.sheets_synced = True
                        obj.sheets_synced_at = now
                        obj.sheets_error = None
                db.commit()
            except Exception as e:
                err = str(e)[:2000]
                app.logger.exception("Sheets falhou (vai ficar pendente): %s", err)
                for tid in tx_ids:
                    obj = db.get(Transaction, tid)
                    if obj:
                        obj.sheets_synced = False
                        obj.sheets_error = err
                db.commit()

            # 5) Feedback pro usuário
            if len(txs) == 1:
                t = txs[0]
                wa_send_text(
                    wa_from,
                    f"✅ Lançamento salvo!\nTipo: {t['type']}\nValor: R$ {t['value']}\nCategoria: {t['category']}\nData: {today}"
                )
            else:
                wa_send_text(wa_from, f"✅ {len(txs)} lançamentos salvos! Data: {today}")

        return "OK", 200
    finally:
        db.close()

@app.post("/tasks/sync-sheets")
def sync_sheets():
    """
    Sincroniza pendências com Sheets.
    Proteção simples por ADMIN_TOKEN.
    """
    if ADMIN_TOKEN:
        token = request.headers.get("X-Admin-Token", "")
        if token != ADMIN_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    limit = int(request.args.get("limit", "200"))

    db = SessionLocal()
    try:
        pending = (
            db.query(Transaction)
            .filter(Transaction.sheets_synced == False)  # noqa: E712
            .order_by(Transaction.created_at.asc())
            .limit(limit)
            .all()
        )
        if not pending:
            return jsonify({"ok": True, "synced": 0, "pending": 0})

        rows = []
        ids = []
        for t in pending:
            rows.append([
                t.wa_from, t.date, t.type, t.value,
                t.category, t.raw_text or "", (t.created_at.isoformat() if t.created_at else "")
            ])
            ids.append(t.id)

        try:
            sheets_append_rows_with_retry(rows, max_retries=4)
            now = dt.datetime.utcnow()
            for tid in ids:
                obj = db.get(Transaction, tid)
                if obj:
                    obj.sheets_synced = True
                    obj.sheets_synced_at = now
                    obj.sheets_error = None
            db.commit()
            return jsonify({"ok": True, "synced": len(ids)})
        except Exception as e:
            err = str(e)[:2000]
            for tid in ids:
                obj = db.get(Transaction, tid)
                if obj:
                    obj.sheets_error = err
            db.commit()
            return jsonify({"ok": False, "error": "sheets_failed", "detail": err}), 500
    finally:
        db.close()
