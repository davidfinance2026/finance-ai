import os
import re
import json
import datetime as dt
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask, request, jsonify

from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import IntegrityError

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# =========================
# Config
# =========================
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v20.0")

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "")

# Nome da aba e range fixo (NÃO faz leitura de metadata)
SHEET_LANC_NAME = os.getenv("SHEET_LANC_NAME", "Lancamentos")
SHEET_LANC_RANGE = os.getenv("SHEET_LANC_RANGE", f"{SHEET_LANC_NAME}!A:H")

DEBUG_LOG_PAYLOAD = os.getenv("DEBUG_LOG_PAYLOAD", "0") == "1"
SECRET_KEY = os.getenv("SECRET_KEY", "")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    DATABASE_URL = "sqlite:///finance_ai.db"  # fallback local

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
    date = Column(String(10), nullable=False)     # YYYY-MM-DD
    type = Column(String(16), nullable=False)     # GASTO/RECEITA
    value = Column(String(32), nullable=False)    # "55.00"
    category = Column(String(64), nullable=False)
    raw_text = Column(Text, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)

    # Sync resiliente pro Sheets
    synced_to_sheets = Column(Boolean, default=False, nullable=False)
    sheets_error = Column(Text, nullable=True)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)


# =========================
# Google Sheets (WRITE-ONLY) via API
# =========================
_sheets_service = None
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_sheets_service():
    """
    Importantíssimo:
    - Não usa gspread (que faz leituras de metadata).
    - Só constrói o client 1x por processo.
    """
    global _sheets_service
    if _sheets_service:
        return _sheets_service

    if not (SPREADSHEET_ID and SERVICE_ACCOUNT_JSON):
        return None

    info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    _sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _sheets_service

def sheets_append_rows(rows):
    """
    rows: list[list[str]]
    Faz append com values.append (WRITE).
    Não faz leitura de spreadsheet/worksheet.
    """
    service = get_sheets_service()
    if not service:
        return

    body = {"values": rows}
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=SHEET_LANC_RANGE,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body
    ).execute()


# =========================
# Util: parse de valor PT-BR
# =========================
_money_re = re.compile(r"[-+]?\d[\d\.]*[,\.]?\d*")

def parse_money_to_decimal(text: str) -> Decimal:
    text = text.strip()
    m = _money_re.search(text)
    if not m:
        raise ValueError("Sem número")
    num = m.group(0)

    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):   # 2.100,00
            num = num.replace(".", "").replace(",", ".")
        else:                                  # 2,100.00
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
        raise ValueError("Valor inválido") from e

def normalize_category(cat: str) -> str:
    cat = cat.strip().lower()
    cat = re.sub(r"\s+", " ", cat)
    # remove palavras “de comando” do começo
    cat = re.sub(r"^(recebi|receber|entrada|ganhei|salario|salário|receita|gastei|gasto|despesa|paguei|saida|saída)\b\s*", "", cat).strip()
    return cat[:64] if cat else "geral"

RECEITA_WORDS = {"recebi", "receber", "entrada", "ganhei", "receita", "salario", "salário"}
GASTO_WORDS   = {"gastei", "gasto", "despesa", "paguei", "saida", "saída"}

def infer_type_from_text(line: str, category: str) -> str:
    t = line.lower()
    c = category.lower()

    # forçar RECEITA quando categoria é salário
    if c in {"salario", "salário"}:
        return "RECEITA"

    if any(w in t.split() for w in RECEITA_WORDS):
        return "RECEITA"
    if any(w in t.split() for w in GASTO_WORDS):
        return "GASTO"

    # default
    return "GASTO"

def parse_line(line: str):
    """
    Aceita formatos:
    - "55 mercado"
    - "55,50 mercado"
    - "2.100,00 salario"
    - "recebi 2100 salario"
    - "receita 2100 salario"
    - "gasto 45 futebol"
    """
    original = line.strip()
    if not original:
        return None

    value = parse_money_to_decimal(original)

    # categoria = texto removendo número e pontuações
    cat = re.sub(_money_re, " ", original)
    cat = re.sub(r"[^\wÀ-ÿ\s]", " ", cat, flags=re.UNICODE)
    cat = re.sub(r"\s+", " ", cat).strip()

    category = normalize_category(cat)

    tx_type = infer_type_from_text(original, category)

    return value, category, tx_type, original

def parse_message_to_transactions(wa_from: str, body_text: str, date_str: str):
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Vazio")

    txs = []
    for ln in lines:
        parsed = parse_line(ln)
        if not parsed:
            continue
        value, category, tx_type, raw = parsed

        txs.append({
            "wa_from": wa_from,
            "date": date_str,
            "type": tx_type,
            "value": str(value.quantize(Decimal("0.01"))),
            "category": category,
            "raw_text": raw,
        })

    if not txs:
        raise ValueError("Nenhuma transação")

    return txs


# =========================
# WhatsApp API
# =========================
def wa_send_text(to: str, text: str):
    if not (WA_ACCESS_TOKEN and WA_PHONE_NUMBER_ID):
        return

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
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

            # 1) Dedup definitivo
            try:
                db.add(ProcessedMessage(msg_id=msg_id, wa_from=wa_from))
                db.commit()
            except IntegrityError:
                db.rollback()
                continue

            # 2) Parse
            try:
                txs = parse_message_to_transactions(wa_from, text, today)
            except Exception:
                wa_send_text(
                    wa_from,
                    "Não entendi. Exemplos:\n"
                    "• 55 mercado\n"
                    "• recebi 2100 salario\n"
                    "• gasto 45 futebol\n"
                    "(pode enviar várias linhas)"
                )
                continue

            # 3) Salva no DB
            created_at = dt.datetime.utcnow().isoformat()
            tx_ids = []
            for t in txs:
                tr = Transaction(
                    wa_from=t["wa_from"],
                    date=t["date"],
                    type=t["type"],
                    value=t["value"],
                    category=t["category"],
                    raw_text=t["raw_text"],
                )
                db.add(tr)
                db.flush()
                tx_ids.append(tr.id)
            db.commit()

            # 4) Tenta sync no Sheets (WRITE only). Se falhar, não derruba.
            try:
                rows = []
                for t in txs:
                    rows.append([
                        t["wa_from"], t["date"], t["type"], t["value"],
                        t["category"], t["raw_text"], created_at, "synced"
                    ])
                sheets_append_rows(rows)

                # marca como synced
                for tid in tx_ids:
                    tr = db.get(Transaction, tid)
                    tr.synced_to_sheets = True
                    tr.sheets_error = None
                db.commit()

            except HttpError as e:
                # 429/403/etc: mantém pendente
                err = str(e)
                app.logger.exception("Sheets HttpError: %s", err)
                for tid in tx_ids:
                    tr = db.get(Transaction, tid)
                    tr.synced_to_sheets = False
                    tr.sheets_error = err[:2000]
                db.commit()

            except Exception as e:
                err = str(e)
                app.logger.exception("Erro ao escrever na planilha: %s", err)
                for tid in tx_ids:
                    tr = db.get(Transaction, tid)
                    tr.synced_to_sheets = False
                    tr.sheets_error = err[:2000]
                db.commit()

            # 5) Feedback
            if len(txs) == 1:
                t = txs[0]
                wa_send_text(
                    wa_from,
                    f"✅ Lançamento salvo!\n"
                    f"Tipo: {t['type']}\n"
                    f"Valor: R$ {t['value']}\n"
                    f"Categoria: {t['category']}\n"
                    f"Data: {today}"
                )
            else:
                wa_send_text(wa_from, f"✅ {len(txs)} lançamentos salvos! Data: {today}")

        return "OK", 200
    finally:
        db.close()

# =========================
# Sync manual (opcional) - resiliente
# =========================
@app.post("/admin/sync_sheets")
def admin_sync_sheets():
    # Proteção simples por header
    if not SECRET_KEY:
        return jsonify({"ok": False, "error": "SECRET_KEY não definido"}), 500

    auth = request.headers.get("X-ADMIN-KEY", "")
    if auth != SECRET_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    db = SessionLocal()
    try:
        pending = db.query(Transaction).filter(Transaction.synced_to_sheets == False).order_by(Transaction.id.asc()).limit(200).all()
        if not pending:
            return jsonify({"ok": True, "synced": 0})

        rows = []
        for tr in pending:
            rows.append([
                tr.wa_from, tr.date, tr.type, tr.value,
                tr.category, tr.raw_text or "", tr.created_at.isoformat(), "synced_late"
            ])

        sheets_append_rows(rows)

        for tr in pending:
            tr.synced_to_sheets = True
            tr.sheets_error = None
        db.commit()

        return jsonify({"ok": True, "synced": len(pending)})
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()
