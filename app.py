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

from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text
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

DATABASE_URL = (os.getenv("DATABASE_URL", "") or "").strip()

# Corrige incompatibilidade comum: "postgres://" -> "postgresql://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if not DATABASE_URL:
    # fallback local (não recomendado em produção)
    DATABASE_URL = "sqlite:///finance_ai.db"


# =========================
# Flask
# =========================
app = Flask(__name__)


# =========================
# DB (Dedup definitivo)
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
    date = Column(String(10), nullable=False)   # YYYY-MM-DD
    type = Column(String(16), nullable=False)   # GASTO/RECEITA
    value = Column(String(32), nullable=False)  # "55.00"
    category = Column(String(64), nullable=False)
    raw_text = Column(Text, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)


# =========================
# Google Sheets (BEST EFFORT)
# - sem leituras repetidas
# - se quota/erro, NÃO derruba WhatsApp
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

    info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    _gspread_client = gspread.authorize(creds)
    return _gspread_client

def _ensure_sheets_ready():
    """
    Roda sob demanda.
    Evita leituras repetidas: após setar _ws_lanc, só append.
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

def sheets_append_transactions(rows, max_retries=3):
    """
    BEST EFFORT: tenta, mas se falhar, só loga e segue.
    Retries com backoff para 429.
    """
    _ensure_sheets_ready()

    for attempt in range(max_retries):
        try:
            _ws_lanc.append_rows(rows, value_input_option="USER_ENTERED")
            return True
        except Exception as e:
            # backoff simples (especialmente para quota 429)
            sleep_s = 2 ** attempt
            app.logger.warning("Sheets append falhou (tentativa %s/%s): %s", attempt+1, max_retries, e)
            time.sleep(sleep_s)

    return False


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

RECEITA_HINTS = {"recebi", "receber", "receita", "salario", "salário", "pagamento", "pix recebido", "entrada"}
GASTO_HINTS = {"gastei", "gasto", "despesa", "paguei", "saída", "saida"}

def guess_type(text: str, category: str) -> str:
    t = text.lower()
    c = category.lower()

    if any(h in t for h in RECEITA_HINTS) or c in {"salario", "salário"}:
        return "RECEITA"
    if any(h in t for h in GASTO_HINTS):
        return "GASTO"

    # default
    return "GASTO"

def parse_lines_to_transactions(wa_from: str, body_text: str, date_str: str):
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
    txs = []

    for ln in lines:
        cleaned = ln.strip()
        value = parse_money_to_decimal(cleaned)

        cat = re.sub(_money_re, " ", cleaned)
        cat = re.sub(r"[^\wÀ-ÿ\s]", " ", cat, flags=re.UNICODE)
        cat = re.sub(r"\s+", " ", cat).strip()

        category = normalize_category(cat) if cat else "geral"
        tx_type = guess_type(cleaned, category)

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
            # 1) Dedup definitivo no Postgres
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

            # 3) Salva no DB (fonte de verdade)
            created_at = dt.datetime.utcnow().isoformat()
            for t in txs:
                db.add(Transaction(
                    wa_from=t["wa_from"],
                    date=t["date"],
                    type=t["type"],
                    value=t["value"],
                    category=t["category"],
                    raw_text=t["raw_text"],
                ))
            db.commit()

            # 4) Planilha (best effort)
            if SPREADSHEET_ID and SERVICE_ACCOUNT_JSON:
                rows = [[
                    t["wa_from"], t["date"], t["type"], t["value"],
                    t["category"], t["raw_text"], created_at
                ] for t in txs]

                ok = sheets_append_transactions(rows)
                if not ok:
                    app.logger.warning("Sheets indisponível/quota. Dados ficaram salvos no Postgres.")

            # 5) Feedback
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
