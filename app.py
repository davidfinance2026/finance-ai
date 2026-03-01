import os
import json
import hmac
import hashlib
import logging
from datetime import datetime, timezone

from flask import Flask, request, jsonify, abort

import gspread
from google.oauth2.service_account import Credentials


# -----------------------------------------------------------------------------
# Config / Logging
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("financeai")

app = Flask(__name__)

WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "").strip()
META_APP_SECRET = os.getenv("META_APP_SECRET", "").strip()

WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "").strip()          # opcional (proteção allowlist)
WA_BUSINESS_ACCOUNT_ID = os.getenv("WA_BUSINESS_ACCOUNT_ID", "").strip()  # opcional (proteção allowlist)

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()

# Se você quiser logar payload (cuidado com dados sensíveis)
DEBUG_LOG_PAYLOAD = os.getenv("DEBUG_LOG_PAYLOAD", "0").strip() in ("1", "true", "True", "yes", "YES")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def safe_str(x) -> str:
    """Converte qualquer coisa para string segura e strip (evita 'int'.strip)."""
    if x is None:
        return ""
    return str(x).strip()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_gspread_client = None


def get_gspread_client() -> gspread.Client:
    global _gspread_client
    if _gspread_client is not None:
        return _gspread_client

    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError("SERVICE_ACCOUNT_JSON não configurado.")

    info = json.loads(SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    _gspread_client = gspread.authorize(creds)
    return _gspread_client


def open_sheet(sheet_name: str):
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID não configurado.")
    gc = get_gspread_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(sheet_name)


def ensure_headers(ws, headers):
    existing = ws.row_values(1)
    if existing != headers:
        ws.clear()
        ws.append_row(headers)


def hmac_sha256_hex(key: str, msg_bytes: bytes) -> str:
    return hmac.new(key.encode("utf-8"), msg_bytes, hashlib.sha256).hexdigest()


def verify_meta_signature(raw_body: bytes) -> bool:
    """
    Proteção #1: valida X-Hub-Signature-256.
    Formato esperado: "sha256=<hexdigest>"
    """
    sig = request.headers.get("X-Hub-Signature-256", "")
    sig = safe_str(sig)

    if not META_APP_SECRET:
        # Segurança: se você quer proteção por assinatura, não aceite sem secret.
        logger.error("META_APP_SECRET vazio -> recusando POST por segurança.")
        return False

    if not sig.startswith("sha256="):
        logger.warning("Header X-Hub-Signature-256 ausente ou inválido.")
        return False

    their = sig.split("=", 1)[1]
    ours = hmac_sha256_hex(META_APP_SECRET, raw_body)
    return hmac.compare_digest(their, ours)


def extract_change_value(payload: dict) -> dict | None:
    """
    Retorna payload['entry'][0]['changes'][0]['value'] se existir.
    """
    try:
        entry = payload.get("entry") or []
        if not entry:
            return None
        changes = entry[0].get("changes") or []
        if not changes:
            return None
        return changes[0].get("value")
    except Exception:
        return None


def allowlist_check(payload: dict) -> bool:
    """
    Proteção #2: valida se o evento é do seu WABA e/ou PHONE_NUMBER_ID.
    - Se variáveis não estiverem setadas, não bloqueia por elas.
    """
    value = extract_change_value(payload) or {}
    metadata = value.get("metadata") or {}

    payload_phone_number_id = safe_str(metadata.get("phone_number_id"))
    payload_waba_id = safe_str(value.get("whatsapp_business_account_id") or (payload.get("id") if isinstance(payload, dict) else ""))

    if WA_PHONE_NUMBER_ID:
        if payload_phone_number_id != WA_PHONE_NUMBER_ID:
            logger.warning(
                "Bloqueado por allowlist phone_number_id. payload=%s expected=%s",
                payload_phone_number_id, WA_PHONE_NUMBER_ID
            )
            return False

    if WA_BUSINESS_ACCOUNT_ID:
        # Nem sempre vem direto; quando vem, costuma estar em entry[0].id (WABA).
        entry = (payload.get("entry") or [{}])[0]
        entry_id = safe_str(entry.get("id"))
        if entry_id and entry_id != WA_BUSINESS_ACCOUNT_ID:
            logger.warning(
                "Bloqueado por allowlist WABA. entry.id=%s expected=%s",
                entry_id, WA_BUSINESS_ACCOUNT_ID
            )
            return False

    return True


def parse_user_message(payload: dict):
    """
    Extrai:
      - wa_id (telefone do usuário)
      - nome (profile.name)
      - message_id
      - texto
    """
    value = extract_change_value(payload) or {}
    contacts = value.get("contacts") or []
    messages = value.get("messages") or []

    wa_id = ""
    profile_name = ""

    if contacts:
        wa_id = safe_str((contacts[0].get("wa_id")))
        profile = contacts[0].get("profile") or {}
        profile_name = safe_str(profile.get("name"))

    if not messages:
        return wa_id, profile_name, "", ""

    msg = messages[0]
    msg_id = safe_str(msg.get("id"))

    # texto pode estar em msg["text"]["body"]
    text = ""
    if msg.get("type") == "text":
        text = safe_str(((msg.get("text") or {}).get("body")))
    else:
        # outros tipos: ignore por enquanto
        text = ""

    return wa_id, profile_name, msg_id, text


def is_duplicate_message(msg_id: str) -> bool:
    """
    Idempotência simples: salva msg_id numa aba "Dedup".
    """
    msg_id = safe_str(msg_id)
    if not msg_id:
        return False

    ws = open_sheet("Dedup")
    ensure_headers(ws, ["message_id", "created_at"])
    # Busca rápida: pega col A inteira (ok para planilhas pequenas/médias).
    col = ws.col_values(1)
    if msg_id in col:
        return True
    ws.append_row([msg_id, utc_now_iso()])
    return False


def get_user_email_by_waid(wa_id: str) -> str | None:
    wa_id = safe_str(wa_id)
    ws = open_sheet("Usuarios")
    ensure_headers(ws, ["wa_id", "email", "nome", "criado_em"])
    rows = ws.get_all_records()
    for r in rows:
        if safe_str(r.get("wa_id")) == wa_id:
            return safe_str(r.get("email")) or None
    return None


def link_user_email(wa_id: str, email: str, nome: str):
    wa_id = safe_str(wa_id)
    email = safe_str(email).lower()
    nome = safe_str(nome)

    ws = open_sheet("Usuarios")
    ensure_headers(ws, ["wa_id", "email", "nome", "criado_em"])

    # se já existe, atualiza
    records = ws.get_all_records()
    for idx, r in enumerate(records, start=2):  # linha 1 = header
        if safe_str(r.get("wa_id")) == wa_id:
            ws.update(f"B{idx}:D{idx}", [[email, nome, utc_now_iso()]])
            return

    ws.append_row([wa_id, email, nome, utc_now_iso()])


def append_lancamento(user_email: str, data: str, tipo: str, categoria: str, descricao: str, valor: float):
    ws = open_sheet("Lancamentos")
    ensure_headers(ws, ["user_email", "data", "tipo", "categoria", "descricao", "valor", "criado_em"])
    ws.append_row([
        safe_str(user_email),
        safe_str(data),
        safe_str(tipo),
        safe_str(categoria),
        safe_str(descricao),
        float(valor),
        utc_now_iso(),
    ])


def parse_lines_to_lancamentos(text: str):
    """
    Converte texto multi-linha em lançamentos.
    Exemplos:
      "55 mercado"
      "recebi 1000 salario"
      "+ 35,90 mercado"
      "- 120 aluguel"
    Retorna lista de dicts: {tipo, valor, categoria, descricao}
    """
    text = safe_str(text)
    lines = [safe_str(l) for l in text.splitlines() if safe_str(l)]
    out = []

    for line in lines:
        low = line.lower()

        tipo = "GASTO"
        if low.startswith("recebi ") or low.startswith("receita ") or low.startswith("+"):
            tipo = "RECEITA"
        if low.startswith("gastei ") or low.startswith("gasto ") or low.startswith("-"):
            tipo = "GASTO"

        # Remove palavras guia
        cleaned = line
        for prefix in ("recebi ", "receita ", "gastei ", "gasto "):
            if cleaned.lower().startswith(prefix):
                cleaned = cleaned[len(prefix):]
                break

        cleaned = cleaned.lstrip("+").lstrip("-").strip()

        # Primeiro token = valor
        parts = cleaned.split()
        if not parts:
            continue

        raw_val = safe_str(parts[0]).replace(".", "").replace(",", ".")
        try:
            valor = float(raw_val)
        except ValueError:
            # não é lançamento, ignore
            continue

        # resto = categoria/descrição
        rest = " ".join(parts[1:]).strip()
        categoria = rest.split()[0].capitalize() if rest else "Outros"
        descricao = rest.capitalize() if rest else categoria

        out.append({
            "tipo": tipo,
            "valor": valor,
            "categoria": categoria,
            "descricao": descricao
        })

    return out


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.get("/webhook/whatsapp")
@app.get("/webhooks/whatsapp")
def whatsapp_verify():
    """
    Verificação do webhook (Meta):
      GET ?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...
    """
    mode = safe_str(request.args.get("hub.mode"))
    token = safe_str(request.args.get("hub.verify_token"))
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token and token == WA_VERIFY_TOKEN:
        return str(challenge), 200

    return "Forbidden", 403


@app.post("/webhook/whatsapp")
@app.post("/webhooks/whatsapp")
def whatsapp_webhook():
    raw = request.get_data() or b""
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    if DEBUG_LOG_PAYLOAD:
        logger.info("===== INCOMING WA WEBHOOK =====")
        logger.info(json.dumps(payload, ensure_ascii=False)[:8000])

    # Proteção #1: assinatura
    if not verify_meta_signature(raw):
        return jsonify({"ok": False, "error": "invalid_signature"}), 401

    # Proteção #2: allowlist (WABA / phone_number_id)
    if not allowlist_check(payload):
        return jsonify({"ok": False, "error": "not_allowed"}), 401

    # parse
    wa_id, profile_name, msg_id, text = parse_user_message(payload)

    # idempotência
    if msg_id and is_duplicate_message(msg_id):
        return jsonify({"ok": True, "dedup": True}), 200

    # Se não tem texto ou não é mensagem
    if not text:
        return jsonify({"ok": True, "ignored": True}), 200

    # Fluxo de vínculo (email)
    user_email = get_user_email_by_waid(wa_id)

    if not user_email:
        # Se a mensagem parece email, vincula
        t = safe_str(text).lower()
        if "@" in t and "." in t and " " not in t:
            link_user_email(wa_id=wa_id, email=t, nome=profile_name)
            return jsonify({"ok": True, "linked": True}), 200
        else:
            # ainda não vinculado -> não registra lançamentos
            return jsonify({"ok": True, "need_link": True}), 200

    # Parse lançamentos
    lancs = parse_lines_to_lancamentos(text)
    if not lancs:
        return jsonify({"ok": True, "no_lancamentos": True}), 200

    # Salvar na planilha
    today = datetime.now().date().isoformat()
    for l in lancs:
        append_lancamento(
            user_email=user_email,
            data=today,
            tipo=l["tipo"],
            categoria=l["categoria"],
            descricao=l["descricao"],
            valor=l["valor"],
        )

    return jsonify({"ok": True, "saved": len(lancs)}), 200


@app.get("/health")
def health():
    return jsonify({"ok": True}), 200


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
