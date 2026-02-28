import os
import json
import requests
from flask import Flask, request, jsonify, make_response

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
# CORS simples (para testes no browser/Hoppscotch)
# =========================
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/<path:_>", methods=["OPTIONS"])
def options_any(_):
    return ("", 204)


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
# Helpers
# =========================
def graph_post_messages(payload: dict) -> tuple[bool, int, str]:
    """
    Faz POST no endpoint /{PHONE_NUMBER_ID}/messages
    Retorna: (ok, status_code, response_text)
    """
    if not WA_ACCESS_TOKEN or not WA_PHONE_NUMBER_ID:
        return (False, 0, "WA_ACCESS_TOKEN ou WA_PHONE_NUMBER_ID não configurado (ENV).")

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=25)
        return (200 <= r.status_code < 300, r.status_code, r.text)
    except Exception as ex:
        return (False, 0, f"Exception requests: {ex}")


def normalize_phone(to: str) -> str:
    """
    Espera E.164 só com dígitos. Ex: 5537998675231
    Remove espaços, +, parênteses, traços.
    """
    digits = "".join(ch for ch in (to or "") if ch.isdigit())
    return digits


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
                "Exemplos:\n"
                "+ 35,90 mercado\n"
                "- 120 aluguel"
            )
            ok, status, resp_text = send_text_message(sender_wa_id, reply)
            print("AUTO_REPLY ok:", ok, "status:", status, "resp:", resp_text)

    return "OK", 200


def extract_messages(payload):
    """
    Extrai mensagens do payload do WhatsApp Cloud API:
    entry[].changes[].value.messages[]
    """
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
# Envio mensagem (Cloud API)
# =========================
def send_text_message(to_wa_id: str, body: str) -> tuple[bool, int, str]:
    """
    Envia TEXTO. Só funciona se:
    - você está na janela de 24h, OU
    - usuário iniciou conversa recentemente.
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": normalize_phone(to_wa_id),
        "type": "text",
        "text": {"body": body},
    }
    ok, status, resp = graph_post_messages(payload)
    print("SEND_TEXT status:", status)
    print("SEND_TEXT resp:", resp)
    return ok, status, resp


def send_template_hello_world(to_wa_id: str) -> tuple[bool, int, str]:
    """
    Envia TEMPLATE padrão 'hello_world' (sandbox/test)
    Funciona fora da janela de 24h.
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": normalize_phone(to_wa_id),
        "type": "template",
        "template": {
            "name": "hello_world",
            "language": {"code": "en_US"}
        }
    }
    ok, status, resp = graph_post_messages(payload)
    print("SEND_TEMPLATE status:", status)
    print("SEND_TEMPLATE resp:", resp)
    return ok, status, resp


# =========================
# Endpoint para testar envio e ver ERRO no Railway Logs
# =========================
@app.post("/send-test")
def send_test():
    """
    POST /send-test
    Body JSON: { "to": "5537998675231", "text": "teste" }
    """
    payload = request.get_json(silent=True) or {}
    to = normalize_phone((payload.get("to") or "").strip())
    text = (payload.get("text") or "Teste do Railway").strip()

    if not to:
        return jsonify({"error": "Campo 'to' é obrigatório. Ex: 5537998675231"}), 400

    ok, status, resp = send_text_message(to, text)
    return jsonify({
        "ok": ok,
        "status": status,
        "response": safe_json_or_text(resp)
    }), 200 if ok else 500


@app.post("/send-template")
def send_template():
    """
    POST /send-template
    Body JSON: { "to": "5537998675231" }
    """
    payload = request.get_json(silent=True) or {}
    to = normalize_phone((payload.get("to") or "").strip())

    if not to:
        return jsonify({"error": "Campo 'to' é obrigatório. Ex: 5537998675231"}), 400

    ok, status, resp = send_template_hello_world(to)
    return jsonify({
        "ok": ok,
        "status": status,
        "response": safe_json_or_text(resp)
    }), 200 if ok else 500


@app.post("/send-any")
def send_any():
    """
    POST /send-any
    Body JSON:
      {
        "to": "5537998675231",
        "text": "Teste backend",
        "fallback_template": true
      }

    Tenta enviar texto.
    Se falhar e fallback_template=true, tenta template hello_world.
    """
    payload = request.get_json(silent=True) or {}
    to = normalize_phone((payload.get("to") or "").strip())
    text = (payload.get("text") or "Teste backend").strip()
    fallback = bool(payload.get("fallback_template", True))

    if not to:
        return jsonify({"error": "Campo 'to' é obrigatório. Ex: 5537998675231"}), 400

    ok, status, resp = send_text_message(to, text)
    if ok:
        return jsonify({"ok": True, "sent": "text", "status": status, "response": safe_json_or_text(resp)}), 200

    if fallback:
        ok2, status2, resp2 = send_template_hello_world(to)
        return jsonify({
            "ok": ok2,
            "sent": "template" if ok2 else "none",
            "text_status": status,
            "text_response": safe_json_or_text(resp),
            "template_status": status2,
            "template_response": safe_json_or_text(resp2),
        }), 200 if ok2 else 500

    return jsonify({
        "ok": False,
        "sent": "none",
        "status": status,
        "response": safe_json_or_text(resp),
    }), 500


def safe_json_or_text(s: str):
    """
    Tenta converter texto JSON em dict pra facilitar ver no Hoppscotch.
    """
    try:
        return json.loads(s)
    except Exception:
        return s


# =========================
# Exec local
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
