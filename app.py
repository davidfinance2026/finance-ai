import os
import json
import time
import re
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify

import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# =========================
# ENV
# =========================
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "")
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v22.0")

# Google Sheets
GSHEET_ID = os.getenv("SPREADSHEET_ID", "")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "")

DEBUG_LOG_PAYLOAD = os.getenv("DEBUG_LOG_PAYLOAD", "false").lower() == "true"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_gs_client = None
_gs_opened = None

# Cache simples p/ reduzir leituras do Sheets
_WA_LINK_CACHE = {}          # phone -> email
_WA_LINK_CACHE_AT = 0.0
_WA_LINK_CACHE_TTL = 30.0    # segundos

EMAIL_RE = re.compile(r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})")

# =========================
# Utils
# =========================
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _today_ymd() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def extract_email(text: str) -> str:
    if not text:
        return ""
    m = EMAIL_RE.search(text.strip())
    return (m.group(1) if m else "").lower().strip()

# =========================
# Google Sheets
# =========================
def get_gs_client() -> gspread.Client:
    global _gs_client
    if _gs_client:
        return _gs_client

    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError("SERVICE_ACCOUNT_JSON não definido no Railway.")

    try:
        sa_info = json.loads(SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as e:
        raise RuntimeError("SERVICE_ACCOUNT_JSON não é um JSON válido.") from e

    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    _gs_client = gspread.authorize(creds)
    return _gs_client

def get_spreadsheet():
    global _gs_opened
    if _gs_opened:
        return _gs_opened

    if not GSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID não definido no Railway.")

    client = get_gs_client()
    _gs_opened = client.open_by_key(GSHEET_ID)
    return _gs_opened

def get_worksheet(title: str):
    ss = get_spreadsheet()
    try:
        return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        return ss.add_worksheet(title=title, rows=2000, cols=20)

def append_row(sheet_name: str, row: list):
    ws = get_worksheet(sheet_name)
    ws.append_row(row, value_input_option="USER_ENTERED")

def ensure_headers(sheet_name: str, headers: list):
    """
    Garante que a linha 1 tenha cabeçalhos. Se estiver vazia, escreve.
    """
    ws = get_worksheet(sheet_name)
    first_row = ws.row_values(1)
    if not first_row:
        ws.append_row(headers, value_input_option="RAW")

# =========================
# WhatsApp <-> Email (Opção B)
# =========================
def _refresh_wa_link_cache(force=False):
    global _WA_LINK_CACHE, _WA_LINK_CACHE_AT

    now = time.time()
    if (not force) and (now - _WA_LINK_CACHE_AT) < _WA_LINK_CACHE_TTL and _WA_LINK_CACHE:
        return

    ws = get_worksheet("WhatsApp")
    # Espera cabeçalhos: wa_number | user_email | criado_em
    # Se já existe, ok. Se não existir, apenas não quebra.
    rows = ws.get_all_values()
    mapping = {}

    if len(rows) >= 2:
        headers = [h.strip().lower() for h in rows[0]]
        # tenta localizar colunas
        def col_idx(name):
            try:
                return headers.index(name)
            except ValueError:
                return -1

        idx_phone = col_idx("wa_number")
        idx_email = col_idx("user_email")

        # fallback por posição se não achar
        if idx_phone < 0: idx_phone = 0
        if idx_email < 0: idx_email = 1

        for r in rows[1:]:
            if not r:
                continue
            phone = (r[idx_phone] if idx_phone < len(r) else "").strip()
            email = (r[idx_email] if idx_email < len(r) else "").strip().lower()
            if phone and email:
                mapping[phone] = email

    _WA_LINK_CACHE = mapping
    _WA_LINK_CACHE_AT = now

def get_linked_email(phone: str) -> str:
    if not phone:
        return ""
    _refresh_wa_link_cache()
    return _WA_LINK_CACHE.get(phone, "")

def upsert_whatsapp_link(phone: str, email: str):
    """
    Salva/atualiza o vínculo na aba WhatsApp:
    wa_number | user_email | criado_em
    """
    ensure_headers("WhatsApp", ["wa_number", "user_email", "criado_em"])
    ws = get_worksheet("WhatsApp")

    # procura na coluna A (wa_number)
    # (assumindo cabeçalho na linha 1)
    col_a = ws.col_values(1)  # inclui header
    target_row = None
    for i, v in enumerate(col_a[1:], start=2):
        if (v or "").strip() == phone:
            target_row = i
            break

    if target_row:
        ws.update(f"B{target_row}", [[email]], value_input_option="RAW")
        # não mexe no criado_em
    else:
        append_row("WhatsApp", [phone, email, _now_iso()])

    # atualiza cache imediatamente
    _refresh_wa_link_cache(force=True)

def ensure_user_exists(email: str, phone: str = ""):
    """
    Garante que exista uma linha na aba Usuarios com este email.
    Cabeçalho esperado (como seu print):
    email | senha | nome | nome_apelido | nome_completo | telefone
    """
    ensure_headers("Usuarios", ["email", "senha", "nome", "nome_apelido", "nome_completo", "telefone"])
    ws = get_worksheet("Usuarios")

    col_a = ws.col_values(1)  # email
    for v in col_a[1:]:
        if (v or "").strip().lower() == email.lower():
            # opcionalmente atualizar telefone se vazio
            if phone:
                # acha linha
                idx = None
                for i, vv in enumerate(col_a[1:], start=2):
                    if (vv or "").strip().lower() == email.lower():
                        idx = i
                        break
                if idx:
                    tel = (ws.cell(idx, 6).value or "").strip()
                    if not tel:
                        ws.update(f"F{idx}", [[phone]], value_input_option="RAW")
            return

    # não existe -> cria uma linha minimalista
    append_row("Usuarios", [email, "", "", "", "", phone])

# =========================
# Parsing de lançamento
# =========================
VALUE_RE = re.compile(r"([-+])?\s*(?:R\$\s*)?(\d+(?:[.,]\d{1,2})?)", re.IGNORECASE)

def normalize_value_to_float(val_str: str) -> float:
    s = val_str.strip()
    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    return float(s)

def parse_lancamento(text: str):
    raw = (text or "").strip()
    if not raw:
        return None

    m = VALUE_RE.search(raw)
    if not m:
        return None

    sign = m.group(1)
    value_part = m.group(2)
    lowered = raw.lower()

    if sign == "+":
        tipo = "RECEITA"
    elif sign == "-":
        tipo = "GASTO"
    else:
        tipo = "RECEITA" if any(k in lowered for k in ["recebi", "receita", "ganhei", "entrada"]) else "GASTO"

    valor = normalize_value_to_float(value_part)

    after = raw[m.end():].strip()
    after = re.sub(r"^[\-\+\:]+", "", after).strip()

    categoria = ""
    descricao = ""
    if after:
        parts = after.split()
        categoria = parts[0].strip().capitalize()
        descricao = " ".join(parts[1:]).strip() or categoria

    data = _today_ymd()

    return {"data": data, "tipo": tipo, "categoria": categoria, "descricao": descricao, "valor": valor}

# =========================
# WhatsApp helpers
# =========================
def wa_send_text(to_phone: str, body: str):
    if not (WA_ACCESS_TOKEN and WA_PHONE_NUMBER_ID):
        raise RuntimeError("WA_ACCESS_TOKEN e/ou WA_PHONE_NUMBER_ID não definidos.")

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }

    r = requests.post(url, headers=headers, json=payload, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Erro ao enviar mensagem WA: {r.status_code} - {r.text}")
    return r.json()

def extract_messages(payload: dict):
    out = []
    try:
        for e in payload.get("entry", []):
            for c in e.get("changes", []):
                value = c.get("value", {})
                for msg in value.get("messages", []):
                    if msg.get("type") == "text":
                        out.append({
                            "from": msg.get("from"),
                            "text": (msg.get("text", {}) or {}).get("body", "")
                        })
    except Exception:
        return []
    return out

# =========================
# Routes
# =========================
@app.get("/")
def home():
    return "ok", 200

@app.get("/debug/env")
def debug_env():
    sa_email = ""
    try:
        if SERVICE_ACCOUNT_JSON:
            sa_email = json.loads(SERVICE_ACCOUNT_JSON).get("client_email", "")
    except Exception:
        sa_email = "(erro ao ler JSON)"

    # tenta contar vínculos
    try:
        _refresh_wa_link_cache(force=True)
        links = len(_WA_LINK_CACHE)
    except Exception:
        links = -1

    return jsonify({
        "GRAPH_VERSION": GRAPH_VERSION,
        "WA_VERIFY_TOKEN_set": bool(WA_VERIFY_TOKEN),
        "WA_ACCESS_TOKEN_set": bool(WA_ACCESS_TOKEN),
        "WA_PHONE_NUMBER_ID_set": bool(WA_PHONE_NUMBER_ID),
        "SPREADSHEET_ID_set": bool(GSHEET_ID),
        "SERVICE_ACCOUNT_JSON_set": bool(SERVICE_ACCOUNT_JSON),
        "SERVICE_ACCOUNT_client_email": sa_email,
        "DEBUG_LOG_PAYLOAD": DEBUG_LOG_PAYLOAD,
        "WA_LINKS_cached": links,
    }), 200

@app.get("/debug/sheets-write")
def debug_sheets_write():
    """
    Teste rápido: tenta escrever 1 linha na aba Lancamentos.
    """
    try:
        ensure_headers("Lancamentos", ["user_email", "data", "tipo", "categoria", "descricao", "valor", "criado_em"])
        test_row = ["debug@test", _today_ymd(), "GASTO", "Teste", "Rota debug", 1.23, _now_iso()]
        append_row("Lancamentos", test_row)
        return jsonify({"ok": True, "written": test_row}), 200
    except Exception as e:
        print("ERROR /debug/sheets-write:", repr(e))
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/webhook")
def webhook_verify():
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == WA_VERIFY_TOKEN and challenge:
        return challenge, 200
    return "Forbidden", 403

@app.post("/webhook")
def webhook_receive():
    payload = request.get_json(silent=True) or {}

    if DEBUG_LOG_PAYLOAD:
        print("======= INCOMING WEBHOOK =======")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("================================")

    msgs = extract_messages(payload)
    if not msgs:
        return jsonify({"ok": True, "message": "no messages"}), 200

    # garante headers principais
    ensure_headers("WhatsApp", ["wa_number", "user_email", "criado_em"])
    ensure_headers("Usuarios", ["email", "senha", "nome", "nome_apelido", "nome_completo", "telefone"])
    ensure_headers("Lancamentos", ["user_email", "data", "tipo", "categoria", "descricao", "valor", "criado_em"])

    for m in msgs:
        from_phone = (m.get("from") or "").strip()
        text = (m.get("text") or "").strip()

        # 1) Verifica se número já está vinculado
        linked_email = get_linked_email(from_phone)

        # 2) Se NÃO estiver vinculado, só aceita email
        if not linked_email:
            email = extract_email(text)

            if not email:
                wa_send_text(
                    from_phone,
                    "🔒 Antes de registrar lançamentos, preciso vincular seu número.\n\n"
                    "Por favor, me envie seu email (ex: nome@dominio.com)."
                )
                continue

            # salva vínculo e garante usuário
            try:
                upsert_whatsapp_link(from_phone, email)
                ensure_user_exists(email, from_phone)

                wa_send_text(
                    from_phone,
                    "✅ Número vinculado com sucesso!\n\n"
                    f"Email: {email}\n\n"
                    "Agora você pode enviar lançamentos.\n\n"
                    "Exemplos:\n"
                    "+ 35,90 mercado\n"
                    "- 120 aluguel\n"
                    "recebi 1000 salario"
                )
            except Exception as e:
                print("ERROR linking:", repr(e))
                wa_send_text(
                    from_phone,
                    "⚠️ Não consegui vincular agora.\n"
                    "Erro: " + str(e)
                )
            continue

        # 3) Se já estiver vinculado, processa lançamento
        lanc = parse_lancamento(text)
        if not lanc:
            wa_send_text(
                from_phone,
                "Não entendi como lançamento.\n\nExemplos:\n"
                "+ 35,90 mercado\n- 120 aluguel\nrecebi 1000 salario"
            )
            continue

        row = [
            linked_email,
            lanc["data"],
            lanc["tipo"],
            lanc["categoria"],
            lanc["descricao"],
            lanc["valor"],
            _now_iso(),
        ]

        try:
            append_row("Lancamentos", row)
            wa_send_text(
                from_phone,
                f"✅ Lançamento registrado!\n"
                f"{lanc['tipo']} • {lanc['categoria']}\n"
                f"{lanc['descricao']}\n"
                f"Valor: R$ {lanc['valor']:.2f}\n"
                f"Data: {lanc['data']}"
            )
        except Exception as e:
            print("ERROR append_row:", repr(e))
            wa_send_text(
                from_phone,
                "⚠️ Não consegui salvar na planilha.\n"
                "Erro: " + str(e) + "\n\n"
                "Verifique se a planilha foi compartilhada com o e-mail do Service Account (client_email)."
            )

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
