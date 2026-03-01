import os
import json
import time
import hmac
import hashlib
from datetime import datetime, timezone

import requests
import gspread
from flask import Flask, request, jsonify, abort

# =========================
# Config (env vars)
# =========================
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")  # para validação do X-Hub-Signature-256

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "")

DEBUG_LOG_PAYLOAD = os.getenv("DEBUG_LOG_PAYLOAD", "0") == "1"

# Abas padrão
SHEET_LANCAMENTOS = os.getenv("SHEET_LANCAMENTOS", "Lancamentos")
SHEET_DEDUP = os.getenv("SHEET_DEDUP", "Dedup")  # <-- CORRETO: Dedup
SHEET_USERS = os.getenv("SHEET_USERS", "Usuarios")
SHEET_PASSWORD_RESETS = os.getenv("SHEET_PASSWORD_RESETS", "PasswordResets")


# =========================
# Helpers: Google Sheets
# =========================
_gspread_client = None
_spreadsheet = None


def get_gspread_client() -> gspread.Client:
    global _gspread_client
    if _gspread_client:
        return _gspread_client

    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError("SERVICE_ACCOUNT_JSON não definido.")

    try:
        info = json.loads(SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as e:
        raise RuntimeError("SERVICE_ACCOUNT_JSON inválido (não é JSON).") from e

    _gspread_client = gspread.service_account_from_dict(info)
    return _gspread_client


def get_spreadsheet():
    global _spreadsheet
    if _spreadsheet:
        return _spreadsheet

    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID não definido.")

    client = get_gspread_client()
    _spreadsheet = client.open_by_key(SPREADSHEET_ID)
    return _spreadsheet


def open_or_create_worksheet(title: str, rows: int = 2000, cols: int = 10):
    sh = get_spreadsheet()
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=str(rows), cols=str(cols))
        return ws


def ensure_headers():
    # Lancamentos
    ws = open_or_create_worksheet(SHEET_LANCAMENTOS, rows=5000, cols=10)
    header = ["user_id", "data", "tipo", "categoria", "descricao", "valor", "criado_em"]
    if ws.row_values(1) != header:
        ws.update("A1:G1", [header])

    # Dedup
    ws_d = open_or_create_worksheet(SHEET_DEDUP, rows=5000, cols=5)
    header_d = ["msg_id", "from", "timestamp", "criado_em", "raw_type"]
    if ws_d.row_values(1) != header_d:
        ws_d.update("A1:E1", [header_d])

    # Usuarios (opcional)
    ws_u = open_or_create_worksheet(SHEET_USERS, rows=5000, cols=10)
    header_u = ["user_id", "nome", "email", "criado_em"]
    if ws_u.row_values(1) != header_u:
        ws_u.update("A1:D1", [header_u])

    # PasswordResets (opcional)
    ws_p = open_or_create_worksheet(SHEET_PASSWORD_RESETS, rows=5000, cols=10)
    header_p = ["email", "token", "expira_em", "criado_em", "usado_em"]
    if ws_p.row_values(1) != header_p:
        ws_p.update("A1:E1", [header_p])


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


def is_duplicate_message(msg_id: str, from_id: str, ts: str, raw_type: str = "") -> bool:
    """
    Dedup simples via Google Sheets:
    - se msg_id já existir na aba Dedup => duplicado
    """
    if not msg_id:
        return False

    ws = open_or_create_worksheet(SHEET_DEDUP, rows=5000, cols=5)

    # Busca na coluna A (msg_id). Para baixo volume funciona bem.
    # (Se crescer, dá pra otimizar com cache em memória + TTL.)
    try:
        col = ws.col_values(1)  # A
    except Exception:
        col = []

    if msg_id in col:
        return True

    # Registrar
    ws.append_row([msg_id, from_id, ts, now_utc_iso(), raw_type], value_input_option="RAW")
    return False


def append_lancamento(user_id: str, data: str, tipo: str, categoria: str, descricao: str, valor: float):
    ws = open_or_create_worksheet(SHEET_LANCAMENTOS, rows=5000, cols=10)
    ws.append_row(
        [user_id, data, tipo, categoria, descricao, float(valor), now_utc_iso()],
        value_input_option="USER_ENTERED"
    )


# =========================
# Helpers: WhatsApp / Meta
# =========================
def verify_signature(req) -> bool:
    """
    Proteção #2: valida X-Hub-Signature-256.
    Se META_APP_SECRET não estiver setado, não valida (não derruba).
    """
    if not META_APP_SECRET:
        return True  # modo "compatível" (mas menos seguro)

    sig = req.headers.get("X-Hub-Signature-256", "")
    if not sig.startswith("sha256="):
        return False

    received = sig.split("=", 1)[1].strip()
    raw_body = req.get_data() or b""

    expected = hmac.new(
        META_APP_SECRET.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(received, expected)


def wa_send_text(to: str, text: str):
    if not (WA_ACCESS_TOKEN and WA_PHONE_NUMBER_ID):
        return

    url = f"https://graph.facebook.com/v21.0/{WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4000]},
    }
    try:
        requests.post(url, headers=headers, json=payload, timeout=15)
    except Exception:
        pass


def extract_messages(payload: dict):
    """
    Extrai mensagens do payload do WhatsApp Cloud API.
    Retorna lista de dicts: {from, id, timestamp, text}
    """
    out = []
    entry = (payload.get("entry") or [])
    for e in entry:
        changes = (e.get("changes") or [])
        for ch in changes:
            value = (ch.get("value") or {})
            messages = (value.get("messages") or [])
            for m in messages:
                msg_from = str(m.get("from") or "")
                msg_id = str(m.get("id") or "")
                ts = str(m.get("timestamp") or "")

                mtype = m.get("type")
                if mtype == "text":
                    body = (m.get("text") or {}).get("body", "")
                    body = str(body)  # <-- evita 'int'.strip
                else:
                    body = ""

                out.append({
                    "from": msg_from,
                    "id": msg_id,
                    "timestamp": ts,
                    "type": str(mtype or ""),
                    "text": body,
                })
    return out


def parse_lancamentos_from_text(text: str):
    """
    Aceita:
      "55 mercado"
      "60 futebol\n55 mercado\n80 internet"
    Retorna lista de tuplas (valor, categoria, descricao).
    """
    items = []
    if text is None:
        return items

    text = str(text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        parts = ln.split()
        if not parts:
            continue

        # primeiro token precisa ser número (aceita 55, 55.5, 55,50, +55, -10)
        raw_val = parts[0].replace(".", "").replace(",", ".")  # 1.234,56 -> 1234.56
        try:
            val = float(raw_val)
        except ValueError:
            continue

        categoria = parts[1] if len(parts) >= 2 else "Geral"
        descricao = " ".join(parts[2:]) if len(parts) >= 3 else ""
        items.append((val, categoria, descricao))
    return items


# =========================
# Flask app
# =========================
def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    # Proteção #1 (verify token) - GET do webhook
    @app.get("/webhooks/whatsapp")
    def whatsapp_verify():
        mode = request.args.get("hub.mode", "")
        token = request.args.get("hub.verify_token", "")
        challenge = request.args.get("hub.challenge", "")

        if mode == "subscribe" and token and token == WA_VERIFY_TOKEN:
            return challenge, 200
        return "Unauthorized", 401

    @app.post("/webhooks/whatsapp")
    def whatsapp_webhook():
        # Proteção #2 (assinatura)
        if not verify_signature(request):
            return "Invalid signature", 403

        payload = request.get_json(silent=True) or {}

        if DEBUG_LOG_PAYLOAD:
            print("===== INCOMING WA WEBHOOK =====")
            print(json.dumps(payload, ensure_ascii=False)[:20000])

        try:
            ensure_headers()

            msgs = extract_messages(payload)
            for m in msgs:
                msg_id = m["id"]
                from_id = m["from"]
                ts = m["timestamp"]
                mtype = m["type"]

                if msg_id and is_duplicate_message(msg_id, from_id, ts, raw_type=mtype):
                    continue

                text = (m.get("text") or "")
                text = str(text).strip()  # <-- garante string

                # Se não for texto, ignora com 200 (Meta exige 200 rápido)
                if not text:
                    continue

                # Data do lançamento (hoje no fuso do servidor; se quiser BRT fixo depois a gente ajusta)
                data = datetime.now().date().isoformat()

                # por padrão, trata como GASTO
                itens = parse_lancamentos_from_text(text)
                if not itens:
                    wa_send_text(from_id, "Não entendi. Ex: 55 mercado (ou várias linhas).")
                    continue

                for valor, categoria, descricao in itens:
                    append_lancamento(
                        user_id=from_id,
                        data=data,
                        tipo="GASTO",
                        categoria=categoria.capitalize(),
                        descricao=descricao,
                        valor=valor
                    )

                # Confirmação
                if len(itens) == 1:
                    valor, categoria, _ = itens[0]
                    wa_send_text(from_id, f"✅ Lançamento salvo!\nTipo: GASTO\nValor: R$ {valor:.2f}\nCategoria: {categoria}\nData: {data}")
                else:
                    wa_send_text(from_id, f"✅ {len(itens)} lançamentos salvos! Data: {data}")

        except Exception as e:
            # Não derrubar o webhook; mas logar para você ver no Railway
            print(f"WA webhook error: {e}")
            return "OK", 200

        return "OK", 200

    return app


# Railway/Gunicorn procura "app" em "app.py" quando você usa: gunicorn app:app
app = create_app()

if __name__ == "__main__":
    # local
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
