import os
import json
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


# =========================
# Rotas básicas
# =========================
@app.get("/")
def health():
    return "OK", 200


@app.get("/debug/env")
def debug_env():
    # NÃO mostra tokens (segurança)
    return jsonify({
        "WA_VERIFY_TOKEN_set": bool(WA_VERIFY_TOKEN),
        "WA_ACCESS_TOKEN_set": bool(WA_ACCESS_TOKEN),
        "WA_PHONE_NUMBER_ID": WA_PHONE_NUMBER_ID,
        "GRAPH_VERSION": GRAPH_VERSION,
        "DEBUG_LOG_PAYLOAD": DEBUG_LOG_PAYLOAD,
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
    payload = request.get_json(silent=True) or {}

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
                "Exemplos:\n"
                "+ 35,90 mercado\n"
                "- 120 aluguel"
            )
            send_text_message(sender_wa_id, reply)

    return "OK", 200


def extract_messages(payload):
    messages = []
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
        print("SEND_TEXT status:", r.status_code)
        print("SEND_TEXT resp:", r.text)
        return ok
    except Exception as ex:
        print("❌ Erro requests send_text_message:", ex)
        return False


# =========================
# Endpoint para testar envio e ver ERRO no Railway Logs
# =========================
@app.post("/send-test")
def send_test():
    """
    Use para testar envio manual:
    POST /send-test
    Body JSON: { "to": "5537998675231", "text": "teste" }
    """
    payload = request.get_json(silent=True) or {}
    to = (payload.get("to") or "").strip()
    text = (payload.get("text") or "Teste do Railway").strip()

    if not to:
        return jsonify({"error": "Campo 'to' é obrigatório. Ex: 5537998675231"}), 400

    ok = send_text_message(to, text)
    return jsonify({"ok": ok}), 200 if ok else 500


# =========================
# Exec local
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
