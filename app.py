import os
import json
import time
import re
import requests
from datetime import datetime, timezone

from flask import Flask, request, jsonify

import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# =========================
# ENV (Railway)
# =========================
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v22.0")
DEBUG_LOG_PAYLOAD = os.getenv("DEBUG_LOG_PAYLOAD", "false").lower() == "true"

GSHEET_ID = os.getenv("GSHEET_ID", "")
GSHEET_TAB = os.getenv("GSHEET_TAB", "Lancamentos")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "")

# =========================
# Google Sheets client (cache)
# =========================
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
_gspread_client = None
_worksheet = None


def get_worksheet():
    global _gspread_client, _worksheet

    if _worksheet is not None:
        return _worksheet

    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError("SERVICE_ACCOUNT_JSON não configurado no Railway.")
    if not GSHEET_ID:
        raise RuntimeError("GSHEET_ID não configurado no Railway.")

    creds_info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_info, scopes=_SCOPES)
    _gspread_client = gspread.authorize(creds)

    sh = _gspread_client.open_by_key(GSHEET_ID)
    ws = sh.worksheet(GSHEET_TAB)

    # Garante cabeçalho
    header = ws.row_values(1)
    expected = ["timestamp", "user_wa", "tipo", "valor", "descricao", "raw"]
    if header != expected:
        # Se estiver vazio, escreve cabeçalho; se tiver outra coisa, não sobrescreve automaticamente
        if len(header) == 0:
            ws.update("A1:F1", [expected])
        else:
            # Opcional: você pode forçar, mas prefiro não mexer no que já existe
            pass

    _worksheet = ws
    return _worksheet


# =========================
# Helpers
# =========================
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")


def parse_money_to_float(s: str):
    """
    Converte:
    "35,90" -> 35.90
    "1.234,56" -> 1234.56
    "1234.56" -> 1234.56
    """
    s = s.strip()

    # Remove moeda e espaços
    s = re.sub(r"[R$r$\s]", "", s, flags=re.IGNORECASE)

    # Se tem vírgula e ponto, assume padrão BR: 1.234,56
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
        return float(s)

    # Se só tem vírgula: 35,90
    if "," in s:
        s = s.replace(",", ".")
        return float(s)

    # Só ponto ou inteiro
    return float(s)


def detect_transaction(text: str):
    """
    Aceita exemplos:
    "+35,90 mercado"
    "-120 aluguel"
    "120 aluguel"
    "35.90 mercado"
    """
    raw = (text or "").strip()
    if not raw:
        return None

    # Normaliza espaços
    raw2 = re.sub(r"\s+", " ", raw)

    # Regex: sinal opcional +/-, valor com separadores, e descrição opcional
    m = re.match(r"^([+\-])?\s*(\d{1,3}(?:[.\s]\d{3})*(?:[,\.\s]\d{1,2})?|\d+(?:[,\.\s]\d{1,2})?)\s*(.*)$", raw2)
    if not m:
        return None

    sign = m.group(1) or ""
    amount_str = m.group(2)
    desc = (m.group(3) or "").strip()

    try:
        value = parse_money_to_float(amount_str)
    except Exception:
        return None

    # Decide tipo
    if sign == "-":
        tipo = "despesa"
    elif sign == "+":
        tipo = "receita"
    else:
        # Sem sinal: assume despesa (padrão)
        tipo = "despesa"

    return {
        "tipo": tipo,
        "valor": round(value, 2),
        "descricao": desc if desc else "(sem descricao)",
        "raw": raw,
    }


def append_to_sheet(user_wa: str, tipo: str, valor: float, descricao: str, raw: str):
    ws = get_worksheet()
    row = [now_iso(), user_wa, tipo, valor, descricao, raw]
    ws.append_row(row, value_input_option="USER_ENTERED")


def wa_send_text(to_number: str, message: str):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message},
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    return r.status_code, r.text


# =========================
# Routes
# =========================
@app.get("/health")
def health():
    return jsonify({"ok": True, "time": now_iso()})


@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403


@app.post("/webhook")
def webhook_receive():
    data = request.get_json(silent=True) or {}

    if DEBUG_LOG_PAYLOAD:
        print("==== INCOMING WEBHOOK ====")
        print(json.dumps(data, ensure_ascii=False))

    # Percorre eventos
    try:
        entry = data.get("entry", [])
        for e in entry:
            changes = e.get("changes", [])
            for c in changes:
                value = c.get("value", {})
                messages = value.get("messages", [])

                for msg in messages:
                    from_wa = msg.get("from")  # número do usuário
                    text_body = (msg.get("text") or {}).get("body", "")

                    # Detecta lançamento
                    tx = detect_transaction(text_body)

                    if tx:
                        # Salva no Sheets
                        append_to_sheet(
                            user_wa=from_wa,
                            tipo=tx["tipo"],
                            valor=tx["valor"],
                            descricao=tx["descricao"],
                            raw=tx["raw"],
                        )
                        reply = (
                            f"✅ Lançamento salvo!\n"
                            f"Tipo: {tx['tipo']}\n"
                            f"Valor: {tx['valor']}\n"
                            f"Desc: {tx['descricao']}"
                        )
                    else:
                        reply = (
                            "Recebi: " + (text_body or "(vazio)") + "\n\n"
                            "Exemplos:\n"
                            "+ 35,90 mercado\n"
                            "- 120 aluguel\n"
                            "120 mercado"
                        )

                    # Responde no WhatsApp
                    wa_send_text(from_wa, reply)

    except Exception as ex:
        print("ERROR webhook:", str(ex))

    # Sempre 200 para o WhatsApp não ficar reenviando
    return jsonify({"ok": True}), 200


@app.get("/debug/env")
def debug_env():
    # NÃO exponha tokens aqui em produção; use só pra teste
    return jsonify({
        "DEBUG_LOG_PAYLOAD": DEBUG_LOG_PAYLOAD,
        "GRAPH_VERSION": GRAPH_VERSION,
        "WA_ACCESS_TOKEN_set": bool(WA_ACCESS_TOKEN),
        "WA_PHONE_NUMBER_ID": WA_PHONE_NUMBER_ID,
        "GSHEET_ID_set": bool(GSHEET_ID),
        "GSHEET_TAB": GSHEET_TAB,
        "SERVICE_ACCOUNT_JSON_set": bool(SERVICE_ACCOUNT_JSON),
    })
