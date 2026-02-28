import os
import json
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# =========================
# Config via ENV (Railway)
# =========================
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")  # ex: 1000378126494307
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v22.0")
DEBUG_LOG_PAYLOAD = os.getenv("DEBUG_LOG_PAYLOAD", "true").lower() == "true"

# Template default
WA_TEMPLATE_NAME = os.getenv("WA_TEMPLATE_NAME", "jaspers_market_order_confirmation_v1")
WA_TEMPLATE_LANG = os.getenv("WA_TEMPLATE_LANG", "en_US")

# =========================
# Debug storage (em memória)
# =========================
LAST_STATUS_EVENT = None
LAST_MESSAGE_EVENT = None

def _now_iso():
    return time.strftime("%Y-%m-%d %H:%M:%S")

# =========================
# Helpers
# =========================
def normalize_phone(phone: str) -> str:
    """WhatsApp Cloud API espera E.164 sem +, sem espaços."""
    if not phone:
        return ""
    return "".join(ch for ch in phone if ch.isdigit())

def graph_post_messages(payload: dict):
    """POST /{PHONE_NUMBER_ID}/messages"""
    if not WA_ACCESS_TOKEN or not WA_PHONE_NUMBER_ID:
        return False, 500, {
            "error": "WA_ACCESS_TOKEN ou WA_PHONE_NUMBER_ID não configurado no ambiente."
        }

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=25)
        status = r.status_code
        try:
            resp = r.json()
        except Exception:
            resp = r.text

        ok = 200 <= status < 300
        return ok, status, resp
    except Exception as ex:
        return False, 500, {"error": f"Erro requests: {str(ex)}"}

def extract_incoming_messages(payload):
    """Extrai lista de mensagens recebidas do webhook."""
    messages = []
    try:
        for e in payload.get("entry", []):
            for c in e.get("changes", []):
                value = c.get("value") or {}
                msgs = value.get("messages") or []
                for m in msgs:
                    messages.append(m)
    except Exception as ex:
        print("Erro ao extrair mensagens:", ex)
    return messages

def extract_statuses(payload):
    """Extrai statuses (sent/delivered/read/failed) do webhook."""
    statuses = []
    try:
        for e in payload.get("entry", []):
            for c in e.get("changes", []):
                value = c.get("value") or {}
                sts = value.get("statuses") or []
                for s in sts:
                    statuses.append(s)
    except Exception as ex:
        print("Erro ao extrair statuses:", ex)
    return statuses

# =========================
# Rotas básicas
# =========================
@app.get("/")
def health():
    return "OK", 200

@app.get("/debug/env")
def debug_env():
    # NÃO mostra tokens
    return jsonify({
        "WA_VERIFY_TOKEN_set": bool(WA_VERIFY_TOKEN),
        "WA_ACCESS_TOKEN_set": bool(WA_ACCESS_TOKEN),
        "WA_PHONE_NUMBER_ID": WA_PHONE_NUMBER_ID,
        "GRAPH_VERSION": GRAPH_VERSION,
        "DEBUG_LOG_PAYLOAD": DEBUG_LOG_PAYLOAD,
        "WA_TEMPLATE_NAME": WA_TEMPLATE_NAME,
        "WA_TEMPLATE_LANG": WA_TEMPLATE_LANG,
    }), 200

@app.get("/debug/last-status")
def debug_last_status():
    return jsonify({
        "last_status": LAST_STATUS_EVENT,
        "last_message": LAST_MESSAGE_EVENT,
    }), 200

# =========================
# Webhook - Verificação (Meta)
# =========================
@app.get("/webhook")
def verify_webhook():
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")

    if DEBUG_LOG_PAYLOAD:
        print("\n========== WEBHOOK VERIFY ==========")
        print("mode:", mode)
        print("token(recebido):", token)
        print("token(ENV set):", bool(WA_VERIFY_TOKEN))
        print("challenge:", challenge)
        print("====================================\n")

    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        return challenge, 200

    return "Forbidden", 403

# =========================
# Webhook - Recebimento (Meta)
# =========================
@app.post("/webhook")
def receive_webhook():
    global LAST_STATUS_EVENT, LAST_MESSAGE_EVENT

    payload = request.get_json(silent=True) or {}

    print("\n========== INCOMING WEBHOOK ==========")
    if DEBUG_LOG_PAYLOAD:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("======================================\n")

    # 1) STATUS updates (sent/delivered/read/failed)
    statuses = extract_statuses(payload)
    for st in statuses:
        # Exemplo de campos:
        # st.get("status") -> sent/delivered/read/failed
        # st.get("id") -> wamid...
        # st.get("recipient_id")
        # st.get("errors") -> lista com code, title, message, etc (quando falha)
        LAST_STATUS_EVENT = {"at": _now_iso(), "data": st}
        print("\n===== STATUS UPDATE =====")
        print("status:", st.get("status"))
        print("id:", st.get("id"))
        print("recipient_id:", st.get("recipient_id"))
        if st.get("errors"):
            print("errors:", json.dumps(st.get("errors"), ensure_ascii=False))
        print("=========================\n")

    # 2) Incoming messages (quando alguém manda msg pro seu número)
    msgs = extract_incoming_messages(payload)
    for msg in msgs:
        LAST_MESSAGE_EVENT = {"at": _now_iso(), "data": msg}
        sender_wa_id = msg.get("from")
        msg_type = msg.get("type")
        text_body = (msg.get("text") or {}).get("body")

        print("\n===== INCOMING MESSAGE =====")
        print("from:", sender_wa_id, "type:", msg_type, "text:", text_body)
        print("============================\n")

        # Responder automaticamente (somente dentro da janela de 24h)
        if msg_type == "text" and sender_wa_id and text_body:
            reply = (
                f"Recebi: {text_body}\n\n"
                "Exemplos:\n"
                "+ 35,90 mercado\n"
                "- 120 aluguel"
            )
            send_text_message(sender_wa_id, reply)

    return "OK", 200

# =========================
# Enviar mensagem de TEXTO
# =========================
def send_text_message(to_wa_id: str, body: str):
    to_wa_id = normalize_phone(to_wa_id)
    payload = {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "text",
        "text": {"body": body},
    }

    ok, status, resp = graph_post_messages(payload)
    print("SEND_TEXT status:", status)
    print("SEND_TEXT resp:", resp)
    return ok, status, resp

# =========================
# Enviar TEMPLATE (3 params)
# =========================
def send_template_message_3params(to_wa_id: str, p1: str, p2: str, p3: str,
                                  template_name: str = None, lang: str = None):
    to_wa_id = normalize_phone(to_wa_id)
    template_name = template_name or WA_TEMPLATE_NAME
    lang = lang or WA_TEMPLATE_LANG

    payload = {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": str(p1)},
                        {"type": "text", "text": str(p2)},
                        {"type": "text", "text": str(p3)},
                    ],
                }
            ],
        },
    }

    ok, status, resp = graph_post_messages(payload)
    print("\n===== SEND TEMPLATE =====")
    print("template:", template_name, "lang:", lang)
    print("to:", to_wa_id)
    print("status:", status)
    print("resp:", resp)
    print("=========================\n")
    return ok, status, resp

# =========================
# Endpoint para testar envio (texto)
# =========================
@app.post("/send-test")
def send_test():
    payload = request.get_json(silent=True) or {}
    to = (payload.get("to") or "").strip()
    text = (payload.get("text") or "Teste do Railway").strip()

    if not to:
        return jsonify({"error": "Campo 'to' é obrigatório. Ex: 5537998675231"}), 400

    ok, status, resp = send_text_message(to, text)
    return jsonify({"ok": ok, "status": status, "response": resp}), (200 if ok else 400)

# =========================
# Endpoint para testar envio (template com 3 params)
# =========================
@app.post("/send-template")
def send_template():
    payload = request.get_json(silent=True) or {}

    to = (payload.get("to") or "").strip()
    if not to:
        return jsonify({"error": "Campo 'to' é obrigatório. Ex: 5537998675231"}), 400

    p1 = payload.get("p1", "David")
    p2 = payload.get("p2", "12345")
    p3 = payload.get("p3", "R$ 99,90")

    template_name = (payload.get("template_name") or "").strip() or None
    lang = (payload.get("lang") or "").strip() or None

    ok, status, resp = send_template_message_3params(
        to_wa_id=to,
        p1=p1, p2=p2, p3=p3,
        template_name=template_name,
        lang=lang
    )

    return jsonify({"ok": ok, "status": status, "response": resp}), (200 if ok else 400)

# =========================
# Exec local
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
