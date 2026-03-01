import os
import json
import hmac
import hashlib
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests
import gspread
from flask import Flask, request, jsonify
from google.oauth2.service_account import Credentials


# =========================================================
# Config
# =========================================================
APP = Flask(__name__)

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("financeai")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()

WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "").strip()
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "").strip()
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "").strip()

# Proteção 2 (assinatura do webhook)
META_APP_SECRET = os.getenv("META_APP_SECRET", "").strip()

DEBUG_LOG_PAYLOAD = os.getenv("DEBUG_LOG_PAYLOAD", "0").strip().lower() in ("1", "true", "yes", "y")

# Abas
LANCAMENTOS_SHEET = os.getenv("LANCAMENTOS_SHEET", "Lancamentos").strip()
DEDUP_SHEET_NAME = os.getenv("DEDUP_SHEET_NAME", "Dedup").strip()

# Cache simples em memória (proteção extra contra duplicado no curto prazo)
_SEEN_CACHE = {}  # msg_id -> epoch_seconds
CACHE_TTL_SECONDS = 60 * 10  # 10 min

_gspread_client = None


# =========================================================
# Helpers Google Sheets
# =========================================================
def get_gspread_client() -> gspread.Client:
    global _gspread_client
    if _gspread_client:
        return _gspread_client

    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError("SERVICE_ACCOUNT_JSON não definido nas variáveis de ambiente.")

    try:
        info = json.loads(SERVICE_ACCOUNT_JSON)
    except Exception as e:
        raise RuntimeError(f"SERVICE_ACCOUNT_JSON inválido: {e}")

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    _gspread_client = gspread.authorize(creds)
    return _gspread_client


def open_spreadsheet():
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID não definido nas variáveis de ambiente.")
    gc = get_gspread_client()
    return gc.open_by_key(SPREADSHEET_ID)


def get_or_create_worksheet(sh, title: str, rows=2000, cols=20):
    """Evita WorksheetNotFound e já cria se não existir."""
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        LOG.warning("Aba '%s' não existe. Criando...", title)
        ws = sh.add_worksheet(title=title, rows=str(rows), cols=str(cols))
        return ws


def ensure_headers(ws, headers):
    """Garante cabeçalhos na primeira linha."""
    try:
        first_row = ws.row_values(1)
    except Exception:
        first_row = []

    if [h.strip() for h in first_row] != headers:
        ws.update("A1", [headers])


# =========================================================
# Helpers Segurança (Proteções)
# =========================================================
def verify_signature(raw_body: bytes) -> bool:
    """
    Valida X-Hub-Signature-256 usando META_APP_SECRET.
    Se META_APP_SECRET estiver vazio, NÃO valida (retorna True) para não travar o app,
    mas loga um alerta.
    """
    if not META_APP_SECRET:
        LOG.warning("META_APP_SECRET vazio. Validação de assinatura desabilitada.")
        return True

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False

    sent_hash = signature.split("sha256=", 1)[1].strip()
    computed = hmac.new(
        META_APP_SECRET.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(computed, sent_hash)


def cleanup_cache(now_ts: int):
    to_delete = [k for k, v in _SEEN_CACHE.items() if now_ts - v > CACHE_TTL_SECONDS]
    for k in to_delete:
        _SEEN_CACHE.pop(k, None)


# =========================================================
# Helpers WhatsApp
# =========================================================
def wa_send_message(to_phone: str, text: str):
    if not (WA_ACCESS_TOKEN and WA_PHONE_NUMBER_ID):
        LOG.error("WA_ACCESS_TOKEN / WA_PHONE_NUMBER_ID não definidos.")
        return

    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code >= 300:
            LOG.error("Erro ao enviar msg WA (%s): %s", r.status_code, r.text)
    except Exception as e:
        LOG.error("Exceção enviando msg WA: %s", e)


# =========================================================
# Parser de lançamentos (texto -> linhas)
# =========================================================
def parse_decimal_br(value_str: str) -> Decimal:
    """
    Aceita:
    - "55"
    - "55,5"
    - "55,50"
    - "1.234,56"
    - "1234.56"
    """
    s = str(value_str).strip()
    if not s:
        raise InvalidOperation("valor vazio")

    # Remove moeda e espaços
    s = s.replace("R$", "").replace(" ", "").strip()

    # Se tem vírgula e ponto, assume padrão BR: 1.234,56
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        # Se só vírgula, vira ponto
        if "," in s:
            s = s.replace(",", ".")

    return Decimal(s)


def parse_lines_to_entries(text: str):
    """
    Espera linhas tipo:
      55 mercado
      60 futebol
      80 internet

    Retorna lista de dicts {tipo, categoria, descricao, valor, data}.
    """
    entries = []
    today = datetime.now(timezone.utc).astimezone().date().isoformat()

    lines = (text or "").splitlines()
    for raw in lines:
        line = str(raw).strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        value_part = parts[0]
        desc = " ".join(parts[1:]).strip()

        try:
            valor = parse_decimal_br(value_part)
        except Exception:
            continue

        entry = {
            "tipo": "GASTO",           # default
            "categoria": desc.title(), # simples
            "descricao": desc,
            "valor": valor,
            "data": today,
        }
        entries.append(entry)

    return entries


# =========================================================
# Dedup (Google Sheet + cache)
# =========================================================
def is_duplicate_message(msg_id: str) -> bool:
    msg_id = str(msg_id).strip()
    if not msg_id:
        return False

    now_ts = int(datetime.now(timezone.utc).timestamp())
    cleanup_cache(now_ts)

    if msg_id in _SEEN_CACHE:
        return True

    sh = open_spreadsheet()
    ws = get_or_create_worksheet(sh, DEDUP_SHEET_NAME, rows=5000, cols=5)
    ensure_headers(ws, ["msg_id", "created_at"])

    try:
        # find levanta CellNotFound se não achar
        ws.find(msg_id)
        # se achou, é duplicado
        _SEEN_CACHE[msg_id] = now_ts
        return True
    except gspread.exceptions.CellNotFound:
        # não existe, então registra
        ws.append_row([msg_id, datetime.now(timezone.utc).isoformat()], value_input_option="RAW")
        _SEEN_CACHE[msg_id] = now_ts
        return False
    except Exception as e:
        # Se sheet der pau, não derruba o webhook; usa cache como fallback
        LOG.error("Dedup falhou (fallback cache): %s", e)
        if msg_id in _SEEN_CACHE:
            return True
        _SEEN_CACHE[msg_id] = now_ts
        return False


def save_entries(user_key: str, entries):
    sh = open_spreadsheet()
    ws = get_or_create_worksheet(sh, LANCAMENTOS_SHEET, rows=5000, cols=20)
    ensure_headers(ws, ["user_email", "data", "tipo", "categoria", "descricao", "valor", "criado_em"])

    created_at = datetime.now(timezone.utc).isoformat()

    rows = []
    for e in entries:
        rows.append([
            user_key,
            e["data"],
            e["tipo"],
            e["categoria"],
            e["descricao"],
            float(e["valor"]),   # grava número
            created_at
        ])

    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")


# =========================================================
# Rotas
# =========================================================
@APP.get("/health")
def health():
    return jsonify({"ok": True})


@APP.get("/webhooks/whatsapp")
def whatsapp_verify():
    """
    Proteção adicional #1:
    verificação do webhook (GET) usando WA_VERIFY_TOKEN.
    """
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")

    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        return challenge, 200
    return "Unauthorized", 401


@APP.post("/webhooks/whatsapp")
def whatsapp_webhook():
    """
    Proteção adicional #2:
    valida assinatura X-Hub-Signature-256 (se META_APP_SECRET estiver definido).
    """
    raw = request.get_data() or b""
    if not verify_signature(raw):
        return "Invalid signature", 401

    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception:
        return "Bad Request", 400

    if DEBUG_LOG_PAYLOAD:
        LOG.info("===== INCOMING WA WEBHOOK =====")
        LOG.info(json.dumps(payload, ensure_ascii=False)[:8000])

    # Estrutura padrão do WA Cloud API:
    # entry[0].changes[0].value.messages[0] etc.
    try:
        entry = (payload.get("entry") or [])[0]
        changes = (entry.get("changes") or [])[0]
        value = changes.get("value") or {}

        messages = value.get("messages") or []
        if not messages:
            # status updates etc.
            return "OK", 200

        msg = messages[0]

        msg_id = str(msg.get("id", "")).strip()
        if msg_id and is_duplicate_message(msg_id):
            return "OK", 200

        from_phone = str(msg.get("from", "")).strip()
        msg_type = str(msg.get("type", "")).strip()

        # Se for texto
        text_body = ""
        if msg_type == "text":
            text_body = (msg.get("text") or {}).get("body", "")
        else:
            text_body = ""

        text_body = str(text_body)  # evita erro de int/None

        entries = parse_lines_to_entries(text_body)
        if not entries:
            wa_send_message(from_phone, "Não entendi. Envie assim:\n55 mercado\n60 futebol\n80 internet")
            return "OK", 200

        # user_key: pode ser o telefone (mais confiável no WA)
        user_key = from_phone

        save_entries(user_key, entries)

        # Resposta amigável
        if len(entries) == 1:
            e = entries[0]
            wa_send_message(
                from_phone,
                f"✅ Lançamento salvo!\nTipo: {e['tipo']}\nValor: R$ {e['valor']}\nCategoria: {e['categoria']}\nData: {e['data']}"
            )
        else:
            wa_send_message(from_phone, f"✅ {len(entries)} lançamentos salvos com sucesso!")

        return "OK", 200

    except Exception as e:
        LOG.exception("WA webhook error: %s", e)
        return "Internal Server Error", 500


# =========================================================
# Main
# =========================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    APP.run(host="0.0.0.0", port=port)
