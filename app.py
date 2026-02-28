import os
import json
import time
import re
import requests
from datetime import datetime, timezone

from flask import Flask, request, jsonify

import gspread
from google.oauth2.service_account import Credentials


# ============================================================
# App
# ============================================================
app = Flask(__name__)


# ============================================================
# ENV (Railway)
# ============================================================
# WhatsApp Cloud API
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
WA_BUSINESS_ACCOUNT_ID = os.getenv("WA_BUSINESS_ACCOUNT_ID", "")
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v22.0")

# Google Sheets
# (você já tem SPREADSHEET_ID no Railway -> vamos usar ela)
GSHEET_ID = os.getenv("SPREADSHEET_ID", "")

# Service account JSON (o seu Railway já tem SERVICE_ACCOUNT_JSON)
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "")

# Debug
DEBUG_LOG_PAYLOAD = os.getenv("DEBUG_LOG_PAYLOAD", "false").lower() == "true"


# ============================================================
# Google Sheets client (cache)
# ============================================================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_gs_client = None
_gs_opened = None


def _gs_now_iso() -> str:
    # ISO em UTC
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_gs_client() -> gspread.Client:
    global _gs_client

    if _gs_client:
        return _gs_client

    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError("SERVICE_ACCOUNT_JSON não definido no ambiente.")

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
        raise RuntimeError("SPREADSHEET_ID não definido no ambiente.")

    client = get_gs_client()
    _gs_opened = client.open_by_key(GSHEET_ID)
    return _gs_opened


def get_worksheet(title: str):
    ss = get_spreadsheet()
    try:
        return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        # cria se não existir
        ws = ss.add_worksheet(title=title, rows=2000, cols=20)
        return ws


def append_row(sheet_name: str, row: list):
    ws = get_worksheet(sheet_name)
    ws.append_row(row, value_input_option="USER_ENTERED")


# ============================================================
# Parsing lançamento (texto livre do WhatsApp)
# Exemplos:
#   "+ 35,90 mercado"
#   "120 aluguel"
#   "- 20 uber"
#   "recebi 1000 salario"
# Regras simples:
#   - se começar com + => RECEITA
#   - se começar com - => GASTO
#   - se não tiver sinal:
#       se tiver palavra "recebi"/"receita"/"ganhei" => RECEITA
#       senão => GASTO
# Valor:
#   aceita "35,90" ou "35.90" ou "R$ 35,90"
# Categoria: última palavra (ou a 2ª depois do valor) de forma simples
# Descrição: resto do texto
# ============================================================
VALUE_RE = re.compile(r"([-+])?\s*(?:R\$\s*)?(\d+(?:[.,]\d{1,2})?)", re.IGNORECASE)


def normalize_value_to_float(val_str: str) -> float:
    # "35,90" -> 35.90 | "35.9" -> 35.9
    v = val_str.strip().replace(".", "").replace(",", ".") if ("," in val_str and "." in val_str) else val_str.replace(",", ".")
    return float(v)


def parse_lancamento(text: str):
    raw = (text or "").strip()
    if not raw:
        return None

    m = VALUE_RE.search(raw)
    if not m:
        return None

    sign = m.group(1)  # +, -, ou None
    value_part = m.group(2)

    # tipo
    lowered = raw.lower()
    if sign == "+":
        tipo = "RECEITA"
    elif sign == "-":
        tipo = "GASTO"
    else:
        if any(k in lowered for k in ["recebi", "receita", "ganhei", "entrada"]):
            tipo = "RECEITA"
        else:
            tipo = "GASTO"

    valor = normalize_value_to_float(value_part)

    # texto depois do valor para categoria/descrição
    after = raw[m.end():].strip()
    after = re.sub(r"^[\-\+\:]+", "", after).strip()

    # categoria: primeira palavra do "after" se existir
    categoria = ""
    descricao = ""

    if after:
        parts = after.split()
        categoria = parts[0].strip().capitalize()
        descricao = " ".join(parts[1:]).strip()
        if not descricao:
            # se só tiver uma palavra, usa ela como descrição também
            descricao = categoria

    # data: hoje (local do servidor) em YYYY-MM-DD
    data = datetime.now().strftime("%Y-%m-%d")

    return {
        "data": data,
        "tipo": tipo,
        "categoria": categoria,
        "descricao": descricao,
        "valor": valor,
    }


# ============================================================
# WhatsApp Cloud API helpers
# ============================================================
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
    """
    Retorna lista de mensagens (texto) e metadados mínimos:
    [
      { "from": "55...", "text": "..." }
    ]
    """
    out = []
    try:
        entry = payload.get("entry", [])
        for e in entry:
            changes = e.get("changes", [])
            for c in changes:
                value = c.get("value", {})
                messages = value.get("messages", [])
                for msg in messages:
                    frm = msg.get("from")
                    mtype = msg.get("type")
                    if mtype == "text":
                        text = (msg.get("text", {}) or {}).get("body", "")
                        out.append({"from": frm, "text": text})
    except Exception:
        return []
    return out


# ============================================================
# Routes
# ============================================================
@app.get("/")
def home():
    return "ok", 200


@app.get("/debug/env")
def debug_env():
    # não vaza tokens; só sinaliza presença
    return jsonify(
        {
            "GRAPH_VERSION": GRAPH_VERSION,
            "WA_VERIFY_TOKEN_set": bool(WA_VERIFY_TOKEN),
            "WA_ACCESS_TOKEN_set": bool(WA_ACCESS_TOKEN),
            "WA_PHONE_NUMBER_ID_set": bool(WA_PHONE_NUMBER_ID),
            "WA_BUSINESS_ACCOUNT_ID_set": bool(WA_BUSINESS_ACCOUNT_ID),
            "SPREADSHEET_ID_set": bool(GSHEET_ID),
            "SERVICE_ACCOUNT_JSON_set": bool(SERVICE_ACCOUNT_JSON),
            "DEBUG_LOG_PAYLOAD": DEBUG_LOG_PAYLOAD,
        }
    ), 200


# Webhook verify (Meta)
@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")

    if mode == "subscribe" and token == WA_VERIFY_TOKEN and challenge:
        return challenge, 200

    return "Forbidden", 403


# Webhook receive
@app.post("/webhook")
def webhook_receive():
    payload = request.get_json(silent=True) or {}

    if DEBUG_LOG_PAYLOAD:
        print("======= INCOMING WEBHOOK =======")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("================================")

    msgs = extract_messages(payload)
    if not msgs:
        # eventos de status etc.
        return jsonify({"ok": True, "message": "no messages"}), 200

    # Processa cada mensagem
    for m in msgs:
        from_phone = m.get("from", "")
        text = (m.get("text") or "").strip()

        # tenta parsear lançamento
        lanc = parse_lancamento(text)

        if not lanc:
            # mensagem de ajuda
            wa_send_text(
                from_phone,
                "Recebi: " + (text or "(vazio)") + "\n\n"
                "Exemplos:\n"
                "+ 35,90 mercado\n"
                "- 120 aluguel\n"
                "recebi 1000 salario\n\n"
                "Envie um valor e uma categoria 🙂",
            )
            continue

        # Salva no Sheets (aba: Lancamentos)
        # Colunas esperadas (pela sua planilha): user_email, data, tipo, categoria, descricao, valor, criado_em
        # Como no WhatsApp não temos email, vamos gravar o número em user_email (ou crie uma coluna própria depois).
        row = [
            from_phone,                 # user_email (temporário: telefone)
            lanc["data"],               # data
            lanc["tipo"],               # tipo
            lanc["categoria"],          # categoria
            lanc["descricao"],          # descricao
            lanc["valor"],              # valor
            _gs_now_iso(),              # criado_em
        ]
        append_row("Lancamentos", row)

        # Confirma pro usuário
        wa_send_text(
            from_phone,
            f"✅ Lançamento registrado!\n"
            f"{lanc['tipo']} • {lanc['categoria']} • {lanc['descricao']}\n"
            f"Valor: R$ {lanc['valor']:.2f}\n"
            f"Data: {lanc['data']}"
        )

    return jsonify({"ok": True}), 200


# ============================================================
# Main (local)
# ============================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
