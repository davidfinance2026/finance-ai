import os
import json
import requests
from flask import Flask, request
from typing import List, Dict, Any

app = Flask(__name__)

# =========================
# Config via ENV (Railway)
# =========================
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")  # ex: 1000378126494307
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v22.0")
DEBUG_LOG_PAYLOAD = os.getenv("DEBUG_LOG_PAYLOAD", "true").lower() == "true"


# =========================
# Rotas básicas
# =========================
@app.get("/")
def health():
    return "OK", 200


# =========================
# Webhook - GET (verificação) e POST (recebimento)
# strict_slashes=False aceita /webhook e /webhook/
# =========================
@app.route("/webhook", methods=["GET", "POST"], strict_slashes=False)
def webhook():
    if request.method == "GET":
        return verify_webhook()
    return receive_webhook()


def verify_webhook():
    """
    Meta/WhatsApp chama:
    GET /webhook?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...
    Se o verify_token bater com WA_VERIFY_TOKEN, devemos retornar o challenge puro.
    """
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")

    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        return challenge, 200

    return "Forbidden", 403


def receive_webhook():
    payload = request.get_json(silent=True) or {}

    if DEBUG_LOG_PAYLOAD:
        print("\n========== INCOMING WEBHOOK ==========")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("======================================\n")

    msgs = extract_messages(payload)

    # Exemplo: responder automaticamente quando receber texto
    for msg in msgs:
        sender_wa_id = msg.get("from")
        msg_type = msg.get("type")
        text_body = (msg.get("text") or {}).get("body")

        if msg_type == "text" and sender_wa_id and text_body:
            reply = (
                f"Recebi: {text_body}\n\n"
                "Envie algo como:\n"
                "+ 35,90 mercado\n"
                "- 120 aluguel"
            )
            send_text_message(sender_wa_id, reply)

    return "OK", 200


# =========================
# Helpers: extrair mensagens
# =========================
def extract_messages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Payload do WhatsApp vem geralmente assim:
    entry -> changes -> value -> messages[]
    """
    messages: List[Dict[str, Any]] = []
    try:
        entry = payload.get("entry", [])
        for e in entry:
            changes = e.get("changes", [])
            for c in changes:
                value = (c.get("value") or {})
                msgs = value.get("messages") or []
                for m in msgs:
                    messages.append(m)
    except Exception as ex:
        print("Erro ao extrair mensagens:", ex)

    return messages


# =========================
# Enviar mensagem (Cloud API)
# =========================
def send_text_message(to_wa_id: str, body: str) -> bool:
    """
    Envia mensagem de texto via WhatsApp Cloud API.
    Requer:
      WA_ACCESS_TOKEN
      WA_PHONE_NUMBER_ID
    """
    if not WA_ACCESS_TOKEN or not WA_PHONE_NUMBER_ID:
        print("⚠️ WA_ACCESS_TOKEN ou WA_PHONE_NUMBER_ID não configurado.")
        return False

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "text",
        "text": {"body": body},
    }

    try:
        r = requests.post(url, headers=headers, json=data, timeout=20)
        ok = 200 <= r.status_code < 300
        if not ok:
            print("❌ Falha ao enviar mensagem:", r.status_code, r.text)
        else:
            print("✅ Mensagem enviada:", r.text)
        return ok
    except Exception as ex:
        print("❌ Erro requests send_text_message:", ex)
        return False


# =========================
# Exec local (não usado no Railway com gunicorn)
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
