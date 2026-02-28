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
# ENV
# =========================
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v22.0")

# Google Sheets
GSHEET_ID = os.getenv("SPREADSHEET_ID", "")  # <-- como você pediu
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "")

DEBUG_LOG_PAYLOAD = os.getenv("DEBUG_LOG_PAYLOAD", "false").lower() == "true"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_gs_client = None
_gs_opened = None

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def get_gs_client() -> gspread.Client:
    global _gs_client
    if _gs_client:
        return _gs_client

    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError("SERVICE_ACCOUNT_JSON não definido no Railway.")

    try:
        sa_info = json.loads(SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as e:
        raise RuntimeError("SERVICE_ACCOUNT_JSON não é um JSON válido.") from e

    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    _gs_client = gspread.authorize(creds)
    return _gs_client

def get_spreadsheet():
    global _gs_opened
    if _gs_opened:
        return _gs_opened

    if not GSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID não definido no Railway.")

    client = get_gs_client()
    _gs_opened = client.open_by_key(GSHEET_ID)
    return _gs_opened

def get_worksheet(title: str):
    ss = get_spreadsheet()
    try:
        return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        # cria se não existir
        return ss.add_worksheet(title=title, rows=2000, cols=20)

def append_row(sheet_name: str, row: list):
    ws = get_worksheet(sheet_name)
    ws.append_row(row, value_input_option="USER_ENTERED")

# =========================
# Parsing simples
# =========================
VALUE_RE = re.compile(r"([-+])?\s*(?:R\$\s*)?(\d+(?:[.,]\d{1,2})?)", re.IGNORECASE)

def normalize_value_to_float(val_str: str) -> float:
    s = val_str.strip()
    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    return float(s)

def parse_lancamento(text: str):
    raw = (text or "").strip()
    if not raw:
        return None

    m = VALUE_RE.search(raw)
    if not m:
        return None

    sign = m.group(1)
    value_part = m.group(2)
    lowered = raw.lower()

    if sign == "+":
        tipo = "RECEITA"
    elif sign == "-":
        tipo = "GASTO"
    else:
        tipo = "RECEITA" if any(k in lowered for k in ["recebi", "receita", "ganhei", "entrada"]) else "GASTO"

    valor = normalize_value_to_float(value_part)

    after = raw[m.end():].strip()
    after = re.sub(r"^[\-\+\:]+", "", after).strip()

    categoria = ""
    descricao = ""
    if after:
        parts = after.split()
        categoria = parts[0].strip().capitalize()
        descricao = " ".join(parts[1:]).strip() or categoria

    data = datetime.now().strftime("%Y-%m-%d")

    return {"data": data, "tipo": tipo, "categoria": categoria, "descricao": descricao, "valor": valor}

# =========================
# WhatsApp helpers
# =========================
def wa_send_text(to_phone: str, body: str):
    if not (WA_ACCESS_TOKEN and WA_PHONE_NUMBER_ID):
        raise RuntimeError("WA_ACCESS_TOKEN e/ou WA_PHONE_NUMBER_ID não definidos.")

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }

    r = requests.post(url, headers=headers, json=payload, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Erro ao enviar mensagem WA: {r.status_code} - {r.text}")
    return r.json()

def extract_messages(payload: dict):
    out = []
    try:
        for e in payload.get("entry", []):
            for c in e.get("changes", []):
                value = c.get("value", {})
                for msg in value.get("messages", []):
                    if msg.get("type") == "text":
                        out.append({
                            "from": msg.get("from"),
                            "text": (msg.get("text", {}) or {}).get("body", "")
                        })
    except Exception:
        return []
    return out

# =========================
# Routes
# =========================
@app.get("/")
def home():
    return "ok", 200

@app.get("/debug/env")
def debug_env():
    # não vaza secrets; só confirma presença
    sa_email = ""
    try:
        if SERVICE_ACCOUNT_JSON:
            sa_email = json.loads(SERVICE_ACCOUNT_JSON).get("client_email", "")
    except Exception:
        sa_email = "(erro ao ler JSON)"

    return jsonify({
        "GRAPH_VERSION": GRAPH_VERSION,
        "WA_VERIFY_TOKEN_set": bool(WA_VERIFY_TOKEN),
        "WA_ACCESS_TOKEN_set": bool(WA_ACCESS_TOKEN),
        "WA_PHONE_NUMBER_ID_set": bool(WA_PHONE_NUMBER_ID),
        "SPREADSHEET_ID_set": bool(GSHEET_ID),
        "SERVICE_ACCOUNT_JSON_set": bool(SERVICE_ACCOUNT_JSON),
        "SERVICE_ACCOUNT_client_email": sa_email,
        "DEBUG_LOG_PAYLOAD": DEBUG_LOG_PAYLOAD,
    }), 200

@app.get("/debug/sheets-write")
def debug_sheets_write():
    """
    Teste rápido: tenta escrever 1 linha na aba Lancamentos.
    Se falhar, retorna o erro.
    """
    try:
        test_row = ["debug@test", datetime.now().strftime("%Y-%m-%d"), "GASTO", "Teste", "Rota debug", 1.23, _now_iso()]
        append_row("Lancamentos", test_row)
        return jsonify({"ok": True, "written": test_row}), 200
    except Exception as e:
        print("ERROR /debug/sheets-write:", repr(e))
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == WA_VERIFY_TOKEN and challenge:
        return challenge, 200
    return "Forbidden", 403

@app.post("/webhook")
def webhook_receive():
    payload = request.get_json(silent=True) or {}

    if DEBUG_LOG_PAYLOAD:
        print("======= INCOMING WEBHOOK =======")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("================================")

    msgs = extract_messages(payload)
    if not msgs:
        return jsonify({"ok": True, "message": "no messages"}), 200

    for m in msgs:
        from_phone = m.get("from", "")
        text = (m.get("text") or "").strip()
        lanc = parse_lancamento(text)

        if not lanc:
            wa_send_text(
                from_phone,
                "Não entendi como lançamento.\n\nExemplos:\n"
                "+ 35,90 mercado\n- 120 aluguel\nrecebi 1000 salario"
            )
            continue

        row = [
            from_phone,
            lanc["data"],
            lanc["tipo"],
            lanc["categoria"],
            lanc["descricao"],
            lanc["valor"],
            _now_iso(),
        ]

        try:
            append_row("Lancamentos", row)
            wa_send_text(
                from_phone,
                f"✅ Registrado!\n{lanc['tipo']} • {lanc['categoria']}\n"
                f"{lanc['descricao']}\nValor: R$ {lanc['valor']:.2f}\nData: {lanc['data']}"
            )
        except Exception as e:
            # Mostra no log e te avisa no WhatsApp com o erro
            print("ERROR append_row:", repr(e))
            wa_send_text(
                from_phone,
                "⚠️ Não consegui salvar na planilha.\n"
                "Erro: " + str(e) + "\n\n"
                "Verifique se a planilha foi compartilhada com o e-mail do Service Account (client_email)."
            )

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
