import os
import json
import hmac
import hashlib
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

# Opcional (recomendado): App Secret do Meta Developers
# Configure no Railway como META_APP_SECRET
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
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits


def verify_meta_signature(req) -> bool:
    """
    Verifica assinatura do webhook (X-Hub-Signature-256).
    Só valida se META_APP_SECRET estiver configurado.
    """
    if not META_APP_SECRET:
        return True  # não valida se você não configurou

    sig = req.headers.get("X-Hub-Signature-256", "")
    if not sig.startswith("sha256="):
        return False

    received = sig.split("sha256=", 1)[1].strip()
    body = req.get_data()  # bytes

    expected = hmac.new(
        META_APP_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(received, expected)


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
    Extrai mensagens recebidas do webhook (value.messages)
    """
    messages = []
    try:
        for e in payload.get("entry", []):
            for c in e.get("changes", []):
                value = c.get("value") or {}
                for m in (value.get("messages") or []):
                    messages.append(m)
    except Exception as ex:
        print("Erro ao extrair messages:", ex)
    return messages


def extract_statuses(payload):
    """
    Extrai statuses do webhook (value.statuses)
    Isso é MUITO importante pra saber se entregou / falhou / motivo.
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
        "META_APP_SECRET_set": bool(META_APP_SECRET),
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
    # (Opcional) Validação de assinatura
    if not verify_meta_signature(request):
        return "Invalid signature", 403

    payload = request.get_json(silent=True) or {}

    print("\n========== INCOMING WEBHOOK ==========")
    if DEBUG_LOG_PAYLOAD:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("======================================\n")

    # 1) Statuses de entrega/falha
    statuses = extract_statuses(payload)
    for st in statuses:
        # exemplos: sent, delivered, read, failed
        print("\n---- STATUS UPDATE ----")
        print(json.dumps(st, ensure_ascii=False, indent=2))
        print("-----------------------\n")

    # 2) Mensagens recebidas
    msgs = extract_messages(payload)

    # Auto-resposta (só funciona se estiver na janela de 24h)
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


# =========================
# Enviar mensagem de TEXTO (Cloud API)
# =========================
def send_text_message(to_wa_id: str, body: str):
    """
    IMPORTANTE:
    - Texto só entrega se o usuário falou com você nas últimas 24h.
    - Para iniciar conversa, use TEMPLATE (send_template_message_3params).
    """
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
def send_template_message_3params(
    to_wa_id: str,
    p1: str,
    p2: str,
    p3: str,
    template_name: str = None,
    lang: str = None
):
    """
    Template com 3 parâmetros no BODY:
      {{1}}, {{2}}, {{3}}
    """
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
@app.get("/send-test")
def send_test_help():
    return jsonify({
        "how_to_use": "Faça POST em /send-test com JSON",
        "example_body": {"to": "5537998675231", "text": "Teste do Railway"},
        "important": "Mensagem TEXT só entrega se o usuário falou com você nas últimas 24h. Para iniciar conversa, use /send-template."
    }), 200


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
@app.get("/send-template")
def send_template_help():
    return jsonify({
        "how_to_use": "Faça POST em /send-template com JSON",
        "example_body": {
            "to": "5537998675231",
            "p1": "David",
            "p2": "Pedido 12345",
            "p3": "R$ 99,90",
            "template_name": "jaspers_market_order_confirmation_v1",
            "lang": "en_US"
        },
        "important": "TEMPLATE é o correto para iniciar conversa (fora da janela de 24h)."
    }), 200


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
        p1=p1,
        p2=p2,
        p3=p3,
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
