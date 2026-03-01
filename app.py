import os
import re
import json
import time
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List, Dict, Any

import requests
from flask import Flask, request, jsonify

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, WorksheetNotFound


# -----------------------------
# Config
# -----------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()

WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "").strip()
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "").strip()
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "").strip()

DEBUG_LOG_PAYLOAD = os.getenv("DEBUG_LOG_PAYLOAD", "0").strip() == "1"

# Nomes das abas
SHEET_LANCAMENTOS = os.getenv("SHEET_LANCAMENTOS", "Lancamentos").strip()
SHEET_USUARIOS = os.getenv("SHEET_USUARIOS", "Usuarios").strip()  # opcional
SHEET_METAS = os.getenv("SHEET_METAS", "Metas").strip()          # opcional
# "Dedup" não é mais obrigatório, dedup será em memória
# mas deixo configurável caso você queira usar no futuro:
SHEET_DEDUP = os.getenv("SHEET_DEDUP", "Dedup").strip()

# Timezone Brasil (BRT = UTC-3)
BRT = timezone(timedelta(hours=-3))


# -----------------------------
# App
# -----------------------------
app = Flask(__name__)


# -----------------------------
# Google Sheets client (cache)
# -----------------------------
_client_cached: Optional[gspread.Client] = None
_sheet_cached: Optional[gspread.Spreadsheet] = None
_ws_cache: Dict[str, gspread.Worksheet] = {}


def get_gspread_client() -> gspread.Client:
    global _client_cached
    if _client_cached:
        return _client_cached

    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError("SERVICE_ACCOUNT_JSON não configurado.")

    try:
        creds_info = json.loads(SERVICE_ACCOUNT_JSON)
    except Exception as e:
        raise RuntimeError("SERVICE_ACCOUNT_JSON inválido (não é JSON).") from e

    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    _client_cached = gspread.authorize(creds)
    return _client_cached


def get_spreadsheet() -> gspread.Spreadsheet:
    global _sheet_cached
    if _sheet_cached:
        return _sheet_cached
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID não configurado.")
    gc = get_gspread_client()
    _sheet_cached = gc.open_by_key(SPREADSHEET_ID)
    return _sheet_cached


def get_or_create_ws(title: str, headers: Optional[List[str]] = None) -> gspread.Worksheet:
    """
    PONTO-CHAVE PARA ESTABILIDADE:
    - Cacheia o objeto worksheet (reduz chamadas).
    - Se não existir, cria.
    - Não faz leituras desnecessárias por mensagem.
    """
    if title in _ws_cache:
        return _ws_cache[title]

    sh = get_spreadsheet()

    # Só aqui tentamos localizar/criar (pode gerar 1-2 reads no boot)
    try:
        ws = sh.worksheet(title)
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=2000, cols=20)
        if headers:
            safe_gspread_call(lambda: ws.append_row(headers, value_input_option="USER_ENTERED"))

    _ws_cache[title] = ws
    return ws


# -----------------------------
# Resiliência: retry/backoff
# -----------------------------
def safe_gspread_call(fn, max_retries: int = 6):
    """
    Retry com backoff exponencial + jitter.
    Alvo: 429/5xx do Google Sheets.
    """
    delay = 0.8
    for attempt in range(max_retries):
        try:
            return fn()
        except APIError as e:
            msg = str(e)
            # gspread APIError normalmente carrega status no texto
            is_quota = ("429" in msg) or ("Quota exceeded" in msg)
            is_5xx = any(code in msg for code in ["500", "502", "503", "504"])

            if not (is_quota or is_5xx):
                raise

            # backoff exponencial com jitter
            jitter = random.uniform(0, 0.35)
            time.sleep(delay + jitter)
            delay *= 2

    # se estourar retries, deixa subir pra log
    return fn()


# -----------------------------
# Dedup DEFINITIVO (sem planilha)
# -----------------------------
# Guardamos IDs recentes em memória com expiração.
# Isso evita chamar Sheets para dedup (que é onde você estourou quota).
_recent_msg_ids: Dict[str, float] = {}
DEDUP_TTL_SECONDS = 60 * 10  # 10 min


def is_duplicate(msg_id: str) -> bool:
    if not msg_id:
        return False
    now = time.time()

    # limpeza leve
    if len(_recent_msg_ids) > 2000:
        cutoff = now - DEDUP_TTL_SECONDS
        for k in list(_recent_msg_ids.keys())[:500]:
            if _recent_msg_ids.get(k, 0) < cutoff:
                _recent_msg_ids.pop(k, None)

    cutoff = now - DEDUP_TTL_SECONDS
    # remove expirados do início (barato)
    for k, ts in list(_recent_msg_ids.items())[:50]:
        if ts < cutoff:
            _recent_msg_ids.pop(k, None)

    if msg_id in _recent_msg_ids:
        return True

    _recent_msg_ids[msg_id] = now
    return False


# -----------------------------
# Parser (mais tolerante)
# -----------------------------
MONEY_RE = re.compile(r"(\d+(?:[.,]\d{1,2})?)")

def normalize_money(s: str) -> float:
    # aceita "55", "55,5", "55.50", "1.234,56", "1234.56"
    s = s.strip()
    s = s.replace(" ", "")

    # caso brasileiro: 1.234,56
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    return float(s)

def detect_tipo_and_rest(text: str) -> Tuple[str, str]:
    """
    Regras simples:
    - se começar com "recebi", "receita", "ganhei" => RECEITA
    - se começar com "paguei", "gastei", "gasto", "despesa" => GASTO
    - default: GASTO (você pode mudar pra perguntar ao usuário)
    """
    t = text.strip().lower()

    for p in ["recebi", "receita", "ganhei", "salário", "salario"]:
        if t.startswith(p):
            rest = text[len(p):].strip()
            return "RECEITA", rest

    for p in ["paguei", "gastei", "gasto", "despesa"]:
        if t.startswith(p):
            rest = text[len(p):].strip()
            return "GASTO", rest

    return "GASTO", text.strip()

def parse_lancamento_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Aceita:
    - "55 mercado"
    - "55,50 mercado"
    - "recebi 2100 salario"
    - "2100 salário"
    - "paguei 45 futebol"
    """
    if not line or not line.strip():
        return None

    # garante string (evita 'int' object has no attribute strip)
    if not isinstance(line, str):
        line = str(line)

    tipo, rest = detect_tipo_and_rest(line)
    # procura primeiro número
    m = MONEY_RE.search(rest)
    if not m:
        return None

    value_raw = m.group(1)
    try:
        valor = normalize_money(value_raw)
    except Exception:
        return None

    # categoria = texto depois do número
    after = rest[m.end():].strip()
    categoria = after if after else "Geral"

    # heurística: se categoria for "salário/salario", força RECEITA
    if categoria.lower() in ["salário", "salario", "pagamento", "recebimento"]:
        tipo = "RECEITA"

    return {"tipo": tipo, "valor": valor, "categoria": categoria}

def parse_message_to_lancamentos(text: str) -> List[Dict[str, Any]]:
    """
    Suporta múltiplas linhas:
    "60 futebol
     55 mercado
     80 internet"
    """
    if not text:
        return []
    if not isinstance(text, str):
        text = str(text)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = []
    for ln in lines:
        item = parse_lancamento_line(ln)
        if item:
            out.append(item)
    return out


# -----------------------------
# Sheets: append sem READ
# -----------------------------
def ensure_headers():
    ws = get_or_create_ws(
        SHEET_LANCAMENTOS,
        headers=["user", "data", "tipo", "categoria", "valor", "origem", "msg_id"]
    )
    return ws

def append_lancamento(user_id: str, tipo: str, categoria: str, valor: float, msg_id: str, origem: str = "whatsapp"):
    ws = ensure_headers()
    data = datetime.now(BRT).date().isoformat()
    row = [user_id, data, tipo, categoria, float(valor), origem, msg_id]

    # append_row = write (não precisa ler planilha)
    safe_gspread_call(lambda: ws.append_row(row, value_input_option="USER_ENTERED"))


# -----------------------------
# WhatsApp send
# -----------------------------
def wa_send_text(to: str, body: str):
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    return r.status_code, r.text


# -----------------------------
# Routes
# -----------------------------
@app.get("/webhooks/whatsapp")
def verify_webhook():
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")

    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403


@app.post("/webhooks/whatsapp")
def whatsapp_webhook():
    payload = request.get_json(silent=True) or {}

    if DEBUG_LOG_PAYLOAD:
        print("INCOMING WA WEBHOOK:", json.dumps(payload, ensure_ascii=False))

    try:
        entry = (payload.get("entry") or [])[0]
        changes = (entry.get("changes") or [])[0]
        value = changes.get("value") or {}

        messages = value.get("messages") or []
        if not messages:
            return jsonify({"ok": True, "ignored": "no_messages"}), 200

        msg = messages[0]
        msg_id = msg.get("id", "")
        if is_duplicate(msg_id):
            return jsonify({"ok": True, "dedup": True}), 200

        from_user = msg.get("from", "")
        msg_type = msg.get("type", "")

        if msg_type != "text":
            wa_send_text(from_user, "No momento eu entendo apenas mensagens de texto. Ex: 55 mercado")
            return jsonify({"ok": True, "ignored": "non_text"}), 200

        text_obj = msg.get("text") or {}
        body = text_obj.get("body", "")
        if not isinstance(body, str):
            body = str(body)

        itens = parse_message_to_lancamentos(body)
        if not itens:
            wa_send_text(from_user, "Não entendi. Ex: 55 mercado (ou várias linhas). Ex: recebi 2100 salario")
            return jsonify({"ok": True, "parsed": 0}), 200

        # salva todos
        saved = 0
        for it in itens:
            append_lancamento(
                user_id=from_user,
                tipo=it["tipo"],
                categoria=it["categoria"],
                valor=it["valor"],
                msg_id=msg_id
            )
            saved += 1

        today = datetime.now(BRT).date().isoformat()
        if saved == 1:
            it = itens[0]
            wa_send_text(
                from_user,
                f"✅ Lançamento salvo!\n"
                f"Tipo: {it['tipo']}\n"
                f"Valor: R$ {it['valor']:.2f}\n"
                f"Categoria: {it['categoria']}\n"
                f"Data: {today}"
            )
        else:
            wa_send_text(from_user, f"✅ {saved} lançamentos salvos! Data: {today}")

        return jsonify({"ok": True, "saved": saved}), 200

    except Exception as e:
        print("WA webhook error:", repr(e))
        # Responde 200 pra evitar re-tentativas agressivas do WhatsApp
        return jsonify({"ok": True, "error": str(e)}), 200


@app.get("/")
def health():
    return "ok", 200


# importante para Gunicorn: precisa existir 'app' no módulo
# (Railway Procfile: web: gunicorn app:app)
