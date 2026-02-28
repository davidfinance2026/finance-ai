import os
import json
import hmac
import hashlib
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

# App Secret (para validar assinatura do webhook)
META_APP_SECRET = os.getenv("META_APP_SECRET", "")

# Template default (pode trocar via body do /send-template)
WA_TEMPLATE_NAME = os.getenv("WA_TEMPLATE_NAME", "jaspers_market_order_confirmation_v1")
WA_TEMPLATE_LANG = os.getenv("WA_TEMPLATE_LANG", "en_US")


# =========================
# Helpers
# =========================
def normalize_phone(phone: str) -> str:
    """
    WhatsApp Cloud API espera E.164 sem +, sem espaços.
    Ex: +55 37 99867-5231 -> 5537998675231
    """
    if not phone:
        return ""
    return "".join(ch for ch in phone if ch.isdigit())


def corsify(resp):
    # Ajuda Hoppscotch/Browser
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        return corsify(make_response("", 200))


def verify_meta_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    Valida X-Hub-Signature-256: sha256=<hex>
    """
    if not META_APP_SECRET:
        # Se não configurou o secret, não bloqueia, mas avisa
        print("⚠️ META_APP_SECRET não configurado. Assinatura do webhook NÃO será validada.")
        return True

    if not signature_header:
        return False

    try:
        algo, their_sig = signature_header.split("=", 1)
        if algo != "sha256":
            return False

        expected = hmac.new(
            META_APP_SECRET.encode("utf-8"),
            msg=raw_body,
            digestmod=hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(expected, their_sig)
    except Exception:
        return False


def graph_post_messages(payload: dict):
    """
    POST /{PHONE_NUMBER_ID}/messages
    Retorna: (ok:bool, status:int, resp_json_or_text:any)
    """
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


def extract_messages(payload):
    """
    Extrai lista de mensagens recebidas do webhook.
    """
    messages = []
    try:
        for e in payload.get("entry", []):
            for c in e.get("changes", []):
                value = c.get("value") or {}
                for m in (value.get("messages") or []):
                    messages.append(m)
    except Exception as ex:
        print("Erro ao extrair mensagens:", ex)
    return messages


def extract_statuses(payload):
    """
    Extrai updates de status: sent/delivered/read/failed
    """
    statuses = []
    try:
        for e in payload.get("entry", []):
            for c in e.get("changes", []):
                value = c.get("value") or {}
                for s in (value.get("statuses") or []):
                    statuses.append(s)
    except Exception as ex:
        print("Erro ao extrair statuses:", ex)
    return statuses


# =========================
# Rotas básicas
# =========================
@app.get("/")
def health():
    return corsify(make_response("OK", 200))


@app.get("/debug/env")
def debug_env():
    # NÃO mostra tokens
    return corsify(jsonify({
        "DEBUG_LOG_PAYLOAD": DEBUG_LOG_PAYLOAD,
        "GRAPH_VERSION": GRAPH_VERSION,
        "META_APP_SECRET_set": bool(META_APP_SECRET),
        "WA_ACCESS_TOKEN_set": bool(WA_ACCESS_TOKEN),
        "WA_PHONE_NUMBER_ID": WA_PHONE_NUMBER_ID,
        "WA_TEMPLATE_LANG": WA_TEMPLATE_LANG,
        "WA_TEMPLATE_NAME": WA_TEMPLATE_NAME,
        "WA_VERIFY_TOKEN_set": bool(WA_VERIFY_TOKEN),
    }))


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
        return corsify(make_response(challenge, 200))

    return corsify(make_response("Forbidden", 403))


# =========================
# Webhook - Recebimento (Meta)
# =========================
@app.post("/webhook")
def receive_webhook():
    raw_body = request.get_data()  # bytes
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not verify_meta_signature(raw_body, signature):
        print("❌ Assinatura inválida no webhook.")
        return corsify(make_response("Invalid signature", 403))

    payload = request.get_json(silent=True) or {}

    print("\n========== INCOMING WEBHOOK ==========")
    if DEBUG_LOG_PAYLOAD:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("======================================\n")

    # Log de statuses (é isso que vai te dizer se entregou ou falhou!)
    statuses = extract_statuses(payload)
    for s in statuses:
        print("\n---- STATUS UPDATE ----")
        print(json.dumps(s, ensure_ascii=False, indent=2))
        print("-----------------------\n")

    msgs = extract_messages(payload)

    # Responde quando receber texto (só funciona na janela de 24h)
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

    return corsify(make_response("OK", 200))


# =========================
# Enviar mensagem de TEXTO (Cloud API)
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
    """
    POST /send-test
    Body JSON: { "to": "5537998675231", "text": "teste" }
    """
    payload = request.get_json(silent=True) or {}
    to = (payload.get("to") or "").strip()
    text = (payload.get("text") or "Teste do Railway").strip()

    if not to:
        return corsify(make_response(jsonify({"error": "Campo 'to' é obrigatório. Ex: 5537998675231"}), 400))

    ok, status, resp = send_text_message(to, text)
    return corsify(make_response(jsonify({"ok": ok, "status": status, "response": resp}), (200 if ok else 400)))


# =========================
# Endpoint para testar envio (template com 3 params)
# =========================
@app.post("/send-template")
def send_template():
    """
    POST /send-template
    Body JSON:
    {
      "to": "5537998675231",
      "p1": "David",
      "p2": "Pedido 12345",
      "p3": "Amanhã 10h",
      "template_name": "jaspers_market_order_confirmation_v1",  # opcional
      "lang": "en_US"                                          # opcional
    }
    """
    payload = request.get_json(silent=True) or {}

    to = (payload.get("to") or "").strip()
    if not to:
        return corsify(make_response(jsonify({"error": "Campo 'to' é obrigatório. Ex: 5537998675231"}), 400))

    p1 = payload.get("p1", "David")
    p2 = payload.get("p2", "12345")
    p3 = payload.get("p3", "Amanhã 10h")

    template_name = (payload.get("template_name") or "").strip() or None
    lang = (payload.get("lang") or "").strip() or None

    ok, status, resp = send_template_message_3params(
        to_wa_id=to,
        p1=p1, p2=p2, p3=p3,
        template_name=template_name,
        lang=lang
    )

    return corsify(make_response(jsonify({"ok": ok, "status": status, "response": resp}), (200 if ok else 400)))


# =========================
# Exec local
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
