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
WA_BUSINESS_ACCOUNT_ID = os.getenv("WA_BUSINESS_ACCOUNT_ID", "")  # opcional (não usado aqui)

GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v22.0")
DEBUG_LOG_PAYLOAD = os.getenv("DEBUG_LOG_PAYLOAD", "true").lower() == "true"

# Coloque aqui o template EXATO que aparece no seu painel Meta
DEFAULT_TEMPLATE_NAME = os.getenv(
    "WA_TEMPLATE_NAME",
    "jaspers_market_order_confirmation_v1"
)
DEFAULT_TEMPLATE_LANG = os.getenv("WA_TEMPLATE_LANG", "en_US")


# =========================
# Helpers
# =========================
def normalize_phone(phone: str) -> str:
    """Mantém só dígitos. Aceita +55... e retorna 55..."""
    if not phone:
        return ""
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits


def graph_post_messages(payload: dict) -> tuple[bool, int, str]:
    if not WA_ACCESS_TOKEN or not WA_PHONE_NUMBER_ID:
        return False, 500, "WA_ACCESS_TOKEN ou WA_PHONE_NUMBER_ID não configurado."

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=25)
        ok = 200 <= r.status_code < 300
        return ok, r.status_code, r.text
    except Exception as ex:
        return False, 500, f"Erro requests: {ex}"


def send_template_message(to_wa_id: str, template_name: str = None, lang_code: str = None) -> tuple[bool, int, str]:
    to_wa_id = normalize_phone(to_wa_id)
    template_name = template_name or DEFAULT_TEMPLATE_NAME
    lang_code = lang_code or DEFAULT_TEMPLATE_LANG

    payload = {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang_code},
        },
    }

    ok, status, resp = graph_post_messages(payload)
    print("\n===== SEND TEMPLATE =====")
    print("to:", to_wa_id)
    print("template:", template_name, lang_code)
    print("status:", status)
    print("resp:", resp)
    print("=========================\n")
    return ok, status, resp


def send_text_message(to_wa_id: str, body: str) -> tuple[bool, int, str]:
    """
    ⚠️ Só entrega se existir conversa aberta (janela 24h) com esse usuário.
    Caso contrário, use template.
    """
    to_wa_id = normalize_phone(to_wa_id)

    payload = {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "text",
        "text": {"body": body},
    }

    ok, status, resp = graph_post_messages(payload)
    print("\n===== SEND TEXT =====")
    print("to:", to_wa_id)
    print("status:", status)
    print("resp:", resp)
    print("=====================\n")
    return ok, status, resp


def extract_all(payload: dict) -> dict:
    """
    Extrai messages (inbound) e statuses (delivery reports).
    """
    out = {"messages": [], "statuses": [], "errors": []}

    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") or {}

                # inbound messages
                for m in (value.get("messages") or []):
                    out["messages"].append(m)

                # delivery status updates
                for s in (value.get("statuses") or []):
                    out["statuses"].append(s)

                # alguns erros podem vir aqui
                if "errors" in value:
                    out["errors"].extend(value.get("errors") or [])

    except Exception as ex:
        out["errors"].append({"exception": str(ex)})

    return out


# =========================
# Rotas básicas
# =========================
@app.get("/")
def health():
    return "OK", 200


@app.get("/debug/env")
def debug_env():
    return jsonify({
        "WA_VERIFY_TOKEN_set": bool(WA_VERIFY_TOKEN),
        "WA_ACCESS_TOKEN_set": bool(WA_ACCESS_TOKEN),
        "WA_PHONE_NUMBER_ID": WA_PHONE_NUMBER_ID,
        "WA_BUSINESS_ACCOUNT_ID_set": bool(WA_BUSINESS_ACCOUNT_ID),
        "GRAPH_VERSION": GRAPH_VERSION,
        "DEBUG_LOG_PAYLOAD": DEBUG_LOG_PAYLOAD,
        "DEFAULT_TEMPLATE_NAME": DEFAULT_TEMPLATE_NAME,
        "DEFAULT_TEMPLATE_LANG": DEFAULT_TEMPLATE_LANG,
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
# Webhook - Recebimento/Status (Meta)
# =========================
@app.post("/webhook")
def receive_webhook():
    payload = request.get_json(silent=True) or {}

    if DEBUG_LOG_PAYLOAD:
        print("\n========== INCOMING WEBHOOK RAW ==========")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("==========================================\n")

    extracted = extract_all(payload)

    # log limpo dos inbound messages
    for msg in extracted["messages"]:
        sender_wa_id = msg.get("from")
        msg_type = msg.get("type")
        text_body = (msg.get("text") or {}).get("body")

        print("\n--- INBOUND MESSAGE ---")
        print("from:", sender_wa_id)
        print("type:", msg_type)
        print("text:", text_body)
        print("-----------------------\n")

    # log limpo de delivery reports (AQUI você vai ver o motivo da não entrega)
    for st in extracted["statuses"]:
        print("\n--- DELIVERY STATUS ---")
        print(json.dumps(st, ensure_ascii=False, indent=2))
        print("-----------------------\n")

    # Se quiser: responder automaticamente quando receber texto (janela 24h)
    for msg in extracted["messages"]:
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
            # responder texto só funciona na janela
            send_text_message(sender_wa_id, reply)

    return "OK", 200


# =========================
# Endpoints de teste
# =========================
@app.post("/send-template")
def send_template():
    """
    POST /send-template
    Body JSON:
      { "to": "5537998675231", "template": "nome_template", "lang": "en_US" }
    """
    payload = request.get_json(silent=True) or {}
    to = (payload.get("to") or "").strip()
    template = (payload.get("template") or "").strip() or None
    lang = (payload.get("lang") or "").strip() or None

    if not to:
        return jsonify({"error": "Campo 'to' é obrigatório. Ex: 5537998675231"}), 400

    ok, status, resp = send_template_message(to, template_name=template, lang_code=lang)
    return jsonify({"ok": ok, "status": status, "response": safe_json(resp)}), (200 if ok else 500)


@app.post("/send-text")
def send_text():
    """
    POST /send-text
    Body JSON:
      { "to": "5537998675231", "text": "oi" }
    ⚠️ Só entrega se a janela de 24h estiver aberta.
    """
    payload = request.get_json(silent=True) or {}
    to = (payload.get("to") or "").strip()
    text = (payload.get("text") or "").strip()

    if not to or not text:
        return jsonify({"error": "Campos 'to' e 'text' são obrigatórios."}), 400

    ok, status, resp = send_text_message(to, text)
    return jsonify({"ok": ok, "status": status, "response": safe_json(resp)}), (200 if ok else 500)


def safe_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return text


# =========================
# Exec local
# =========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
