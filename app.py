import os
import json
import re
import time
import hmac
import hashlib
import requests
import gspread

from flask import Flask, render_template, request, jsonify, session, send_from_directory
from google.oauth2.service_account import Credentials
from datetime import datetime, date, timezone

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "financeai-secret")
app.config["JSON_AS_ASCII"] = False

# =========================
# Resposta UTF-8 + no-cache HTML
# =========================
@app.after_request
def headers_fix(response):
    mt = (response.mimetype or "").lower()

    if mt in ("text/html", "text/plain", "text/css", "application/javascript", "text/javascript"):
        response.headers["Content-Type"] = f"{mt}; charset=utf-8"
    elif mt == "application/json":
        response.headers["Content-Type"] = "application/json; charset=utf-8"
    elif mt.startswith("text/"):
        response.headers["Content-Type"] = f"{mt}; charset=utf-8"

    if mt == "text/html":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"

    return response


@app.get("/robots.txt")
def robots_txt():
    return send_from_directory("static", "robots.txt")


# =========================
# ENV / CONSTANTES
# =========================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Planilha
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")  # preferido
SHEET_NAME = os.environ.get("SHEET_NAME", "Controle Financeiro")  # fallback por nome

ABA_USUARIOS = "Usuarios"
ABA_LANCAMENTOS = "Lancamentos"
ABA_WHATSAPP = "WhatsApp"
ABA_METAS = "Metas"
ABA_INVESTIMENTOS = "Investimentos"
ABA_PASSWORD_RESETS = "PasswordResets"

# WhatsApp Cloud API
WA_VERIFY_TOKEN = os.environ.get("WA_VERIFY_TOKEN", "")
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_ACCESS_TOKEN = os.environ.get("WA_ACCESS_TOKEN", "")
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v20.0")

# Proteção 1: assinatura do Meta
META_APP_SECRET = os.environ.get("META_APP_SECRET", "")  # coloque no Railway

# Proteção 2: dedupe (idempotência)
DEDUP_TTL_SECONDS = int(os.environ.get("DEDUP_TTL_SECONDS", "900"))  # 15 min padrão

# Log opcional
DEBUG_LOG_PAYLOAD = os.environ.get("DEBUG_LOG_PAYLOAD", "false").lower() == "true"


# =========================
# Google Sheets helpers
# =========================
_client = None
_spreadsheet = None

def _utc_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def get_client():
    global _client
    if _client:
        return _client

    creds_json = os.environ.get("SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise Exception("SERVICE_ACCOUNT_JSON não configurado")

    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    _client = gspread.authorize(creds)
    return _client

def open_spreadsheet():
    global _spreadsheet
    if _spreadsheet:
        return _spreadsheet

    c = get_client()
    if SPREADSHEET_ID:
        _spreadsheet = c.open_by_key(SPREADSHEET_ID)
    else:
        _spreadsheet = c.open(SHEET_NAME)
    return _spreadsheet

def get_sheet(nome_aba):
    sh = open_spreadsheet()
    return sh.worksheet(nome_aba)

def get_or_create_sheet(nome_aba, headers):
    sh = open_spreadsheet()
    try:
        ws = sh.worksheet(nome_aba)
    except Exception:
        ws = sh.add_worksheet(title=nome_aba, rows=2000, cols=max(len(headers) + 5, 10))
        # headers na linha 1
        ws.update(f"A1:{chr(64+len(headers))}1", [headers])
        return ws

    cur = ws.row_values(1)
    if cur != headers:
        ws.update(f"A1:{chr(64+len(headers))}1", [headers])
    return ws

def ensure_headers():
    # Mantém TODAS as abas que você citou (cria se não existir)
    get_or_create_sheet(ABA_USUARIOS, ["email","senha","nome_apelido","nome_completo","telefone","criado_em"])
    get_or_create_sheet(ABA_LANCAMENTOS, ["user_email","data","tipo","categoria","descricao","valor","criado_em"])
    get_or_create_sheet(ABA_WHATSAPP, ["wa_number","user_email","criado_em"])
    get_or_create_sheet(ABA_METAS, ["user_email","titulo","valor_alvo","prazo","status","criado_em"])
    get_or_create_sheet(ABA_INVESTIMENTOS, ["user_email","ativo","tipo","valor","data","obs","criado_em"])
    get_or_create_sheet(ABA_PASSWORD_RESETS, ["email","token","expira_em","usado_em","criado_em"])


# =========================
# Utils
# =========================
def require_login():
    return session.get("user")

def normalize_wa_number(raw: str) -> str:
    s = (raw or "").strip().replace("+", "")
    s = re.sub(r"[^0-9]", "", s)
    return s

def parse_money(v):
    if v is None:
        return 0.0
    s = str(v).strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    s = re.sub(r"[^0-9\.\-]", "", s)
    try:
        return float(s)
    except:
        return 0.0


# =========================
# WhatsApp: vínculo número -> email
# =========================
def find_user_by_wa(wa_number: str):
    ensure_headers()
    ws = get_sheet(ABA_WHATSAPP)
    rows = ws.get_all_records()
    target = normalize_wa_number(wa_number)
    for r in rows:
        if normalize_wa_number(r.get("wa_number")) == target:
            return str(r.get("user_email") or "").lower().strip() or None
    return None

def link_wa_to_email(wa_number: str, email: str):
    ensure_headers()
    ws = get_sheet(ABA_WHATSAPP)
    wa_number = normalize_wa_number(wa_number)
    email = (email or "").lower().strip()

    if not wa_number or not email:
        return False, "Número e email são obrigatórios."

    rows = ws.get_all_records()
    for i, r in enumerate(rows, start=2):
        if normalize_wa_number(r.get("wa_number")) == wa_number:
            ws.update_cell(i, 2, email)
            return True, f"✅ Número atualizado para {email}."
    ws.append_row([wa_number, email, _utc_iso()], value_input_option="USER_ENTERED")
    return True, f"✅ Número vinculado com sucesso!\n\nEmail: {email}"

def unlink_wa(wa_number: str):
    ensure_headers()
    ws = get_sheet(ABA_WHATSAPP)
    wa_number = normalize_wa_number(wa_number)
    rows = ws.get_all_records()
    for i, r in enumerate(rows, start=2):
        if normalize_wa_number(r.get("wa_number")) == wa_number:
            ws.delete_rows(i)
            return True, "✅ Número desconectado."
    return False, "Esse número não estava conectado."


# =========================
# WhatsApp Cloud API: envio
# =========================
def wa_send_text(to_number: str, text: str):
    if not (WA_PHONE_NUMBER_ID and WA_ACCESS_TOKEN):
        print("WA creds missing. Would send:", text)
        return

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": normalize_wa_number(to_number),
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    if r.status_code >= 400:
        print("WA send error:", r.status_code, r.text)


# =========================
# Parser de comando financeiro (robusto)
# =========================
def parse_finance_command(text):
    """
    Aceita:
      - "gasto 32,90 mercado"
      - "receita 2500 salario"
      - "32,90 mercado" (assume gasto)
      - "+ 35,90 mercado" (receita)
      - "- 120 aluguel" (gasto)
      - "recebi 1000 salario" (receita)
    """
    if text is None:
        return None

    # proteção: se vier algo não-string (ex: int/dict), converte
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except Exception:
            text = str(text)

    t = text.strip()
    t = re.sub(r"\s+", " ", t)
    low = t.lower()

    # tipo por prefixo
    tipo = None
    t2 = t

    if low.startswith(("gasto ", "despesa ")):
        tipo = "GASTO"
        t2 = t.split(" ", 1)[1].strip()
    elif low.startswith(("receita ", "entrada ")):
        tipo = "RECEITA"
        t2 = t.split(" ", 1)[1].strip()
    elif low.startswith(("recebi ", "ganhei ", "salario ")):
        tipo = "RECEITA"
        t2 = t.split(" ", 1)[1].strip()

    # tipo por sinal
    if t2.startswith("+"):
        tipo = "RECEITA"
        t2 = t2[1:].strip()
    elif t2.startswith("-"):
        tipo = "GASTO"
        t2 = t2[1:].strip()

    if not tipo:
        tipo = "GASTO"

    # achar primeiro número (aceita 1.234,56 ou 1234.56 ou 1234,56)
    m = re.search(r"(-?\d{1,3}(?:\.\d{3})*(?:,\d{1,2})|-?\d+(?:[\.,]\d{1,2})?)", t2)
    if not m:
        return None

    valor_raw = m.group(1)
    valor = parse_money(valor_raw)

    rest = (t2[m.end():] or "").strip(" -–—:;")
    if not rest:
        categoria = "Geral"
        descricao = ""
    else:
        parts = rest.split(" ", 1)
        categoria = (parts[0] or "Geral").strip().title()
        descricao = parts[1].strip() if len(parts) > 1 else categoria

    return {
        "tipo": tipo,
        "valor": float(valor),
        "categoria": categoria,
        "descricao": descricao,
        "data": date.today().isoformat(),
    }


# =========================
# PROTEÇÃO 1: Validar assinatura do Meta
# =========================
def verify_meta_signature(req) -> bool:
    """
    Meta envia: X-Hub-Signature-256: sha256=<hex>
    Calculamos HMAC SHA256 do corpo raw com META_APP_SECRET.
    """
    # Se você não configurou o secret, não bloqueia (compatível)
    if not META_APP_SECRET:
        return True

    sig = req.headers.get("X-Hub-Signature-256", "")
    if not sig or not sig.startswith("sha256="):
        return False

    their_hex = sig.split("sha256=", 1)[1].strip()
    raw = req.get_data() or b""

    mac = hmac.new(META_APP_SECRET.encode("utf-8"), msg=raw, digestmod=hashlib.sha256)
    our_hex = mac.hexdigest()

    return hmac.compare_digest(our_hex, their_hex)


# =========================
# PROTEÇÃO 2: Deduplicação
# =========================
_processed = {}  # key -> timestamp

def _dedup_cleanup(now=None):
    now = now or time.time()
    dead = []
    for k, ts in _processed.items():
        if now - ts > DEDUP_TTL_SECONDS:
            dead.append(k)
    for k in dead:
        _processed.pop(k, None)

def is_duplicate(key: str) -> bool:
    now = time.time()
    _dedup_cleanup(now)
    if key in _processed:
        return True
    _processed[key] = now
    return False


# =========================
# Webhook WhatsApp
# =========================
@app.get("/webhooks/whatsapp")
def wa_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    # verificação padrão do Meta
    if mode == "subscribe" and token and token == WA_VERIFY_TOKEN:
        return challenge or "", 200
    return "forbidden", 403


@app.post("/webhooks/whatsapp")
def wa_webhook():
    # Proteção 1: assinatura
    if not verify_meta_signature(request):
        return "invalid signature", 403

    payload = request.get_json(silent=True) or {}

    if DEBUG_LOG_PAYLOAD:
        print("======= INCOMING WA WEBHOOK =======")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("===================================")

    try:
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}

                # mensagens
                for msg in (value.get("messages", []) or []):
                    msg_id = msg.get("id") or ""
                    from_number = normalize_wa_number(msg.get("from"))
                    msg_type = msg.get("type")

                    # Proteção 2: dedupe por message id (se vier)
                    if msg_id and is_duplicate(f"msg:{msg_id}"):
                        continue

                    # texto
                    if msg_type == "text":
                        body = ((msg.get("text") or {}) or {}).get("body", "")
                        if body is None:
                            body = ""

                        # garante string
                        if not isinstance(body, str):
                            body = str(body)

                        cmd = body.strip()
                        low = cmd.lower()

                        # fluxo vincular
                        if low.startswith("conectar "):
                            email = cmd.split(" ", 1)[1].strip()
                            _, resp = link_wa_to_email(from_number, email)
                            wa_send_text(
                                from_number,
                                resp + "\n\nAgora você pode enviar lançamentos.\n"
                                       "Exemplos:\n"
                                       "• gasto 32,90 mercado\n"
                                       "• receita 2500 salario\n"
                                       "• 32,90 mercado"
                            )
                            continue

                        if low in ("desconectar", "desconectar whatsapp"):
                            _, resp = unlink_wa(from_number)
                            wa_send_text(from_number, resp)
                            continue

                        # precisa estar vinculado
                        user_email = find_user_by_wa(from_number)
                        if not user_email:
                            wa_send_text(
                                from_number,
                                "🔒 Antes de registrar lançamentos, preciso vincular seu número.\n\n"
                                "Por favor, me envie seu email (ex: nome@dominio.com).\n"
                                "Ou use: conectar SEU_EMAIL_DO_APP"
                            )
                            continue

                        parsed = parse_finance_command(cmd)
                        if not parsed:
                            wa_send_text(
                                from_number,
                                "Não entendi 😅\n\nUse assim:\n"
                                "• gasto 32,90 mercado\n"
                                "• receita 2500 salario\n"
                                "• 32,90 mercado (assume gasto)\n"
                                "• + 35,90 mercado\n"
                                "• - 120 aluguel"
                            )
                            continue

                        # salva na planilha
                        ensure_headers()
                        ws_lanc = get_sheet(ABA_LANCAMENTOS)
                        ws_lanc.append_row([
                            user_email,
                            parsed["data"],
                            parsed["tipo"],
                            parsed["categoria"],
                            parsed["descricao"],
                            float(parsed["valor"]),
                            _utc_iso()
                        ], value_input_option="USER_ENTERED")

                        valor_fmt = f"{parsed['valor']:.2f}".replace(".", ",")
                        wa_send_text(
                            from_number,
                            f"✅ Lançamento salvo!\n"
                            f"Tipo: {parsed['tipo']}\n"
                            f"Valor: R$ {valor_fmt}\n"
                            f"Categoria: {parsed['categoria']}\n"
                            f"Data: {parsed['data']}"
                        )
                        continue

                    # mídia (opcional)
                    if msg_type in ("image", "document", "audio", "video"):
                        media = msg.get(msg_type, {}) or {}
                        media_id = media.get("id")
                        caption = (media.get("caption") or "").strip() if isinstance(media.get("caption"), str) else ""

                        user_email = find_user_by_wa(from_number)
                        if not user_email:
                            wa_send_text(from_number, "🔒 Conecte primeiro: conectar SEU_EMAIL_DO_APP")
                            continue

                        ensure_headers()
                        ws_lanc = get_sheet(ABA_LANCAMENTOS)
                        ws_lanc.append_row([
                            user_email,
                            date.today().isoformat(),
                            "GASTO",
                            "Comprovante",
                            f"{caption} [MID:{media_id}]".strip(),
                            0.0,
                            _utc_iso()
                        ], value_input_option="USER_ENTERED")

                        wa_send_text(
                            from_number,
                            "📎 Comprovante recebido!\n"
                            "Salvei como 'Comprovante' (valor 0,00) para você editar depois no app."
                        )
                        continue

        return "ok", 200

    except Exception as e:
        print("WA webhook error:", str(e))
        return "ok", 200


# =========================
# Web app
# =========================
@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/register")
def register():
    ensure_headers()
    data = request.get_json(force=True) or {}

    email = str(data.get("email", "")).lower().strip()
    senha = str(data.get("senha", ""))
    confirmar = str(data.get("confirmar_senha", ""))

    nome_apelido = str(data.get("nome_apelido", ""))
    nome_completo = str(data.get("nome_completo", ""))
    telefone = str(data.get("telefone", ""))

    if not email or not senha:
        return jsonify(error="Email e senha obrigatórios"), 400
    if senha != confirmar:
        return jsonify(error="Senhas não conferem"), 400

    ws = get_sheet(ABA_USUARIOS)
    emails = [e.lower().strip() for e in ws.col_values(1)]
    if email in emails:
        return jsonify(error="Email já cadastrado"), 400

    ws.append_row([email, senha, nome_apelido, nome_completo, telefone, _utc_iso()], value_input_option="USER_ENTERED")
    session["user"] = email
    return jsonify(email=email)


@app.post("/api/login")
def login():
    ensure_headers()
    data = request.get_json(force=True) or {}
    email = str(data.get("email", "")).lower().strip()
    senha = str(data.get("senha", ""))

    ws = get_sheet(ABA_USUARIOS)
    rows = ws.get_all_records()
    for r in rows:
        if str(r.get("email", "")).lower().strip() == email and str(r.get("senha", "")) == senha:
            session["user"] = email
            return jsonify(email=email)
    return jsonify(error="Email ou senha inválidos"), 401


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/reset_password")
def reset_password():
    ensure_headers()
    data = request.get_json(force=True) or {}
    email = str(data.get("email", "")).lower().strip()
    nova = str(data.get("nova_senha", ""))
    conf = str(data.get("confirmar", ""))

    if not email or not nova:
        return jsonify(error="Email e nova senha obrigatórios"), 400
    if nova != conf:
        return jsonify(error="Senhas não conferem"), 400

    ws = get_sheet(ABA_USUARIOS)
    rows = ws.get_all_records()
    for i, r in enumerate(rows, start=2):
        if str(r.get("email", "")).lower().strip() == email:
            ws.update_cell(i, 2, nova)
            return jsonify(ok=True)
    return jsonify(error="Email não encontrado"), 404


@app.get("/api/lancamentos")
def listar_lancamentos():
    user = require_login()
    if not user:
        return jsonify(error="Não logado"), 401

    limit = int(request.args.get("limit", 50))
    ws = get_sheet(ABA_LANCAMENTOS)
    rows = ws.get_all_records()

    items = []
    for idx, r in enumerate(rows, start=2):
        if str(r.get("user_email", "")).lower().strip() == user:
            r["row"] = idx
            items.append(r)

    items.sort(key=lambda x: x.get("data", ""), reverse=True)
    return jsonify(items=items[:limit])


@app.post("/api/lancamentos")
def criar_lancamento():
    user = require_login()
    if not user:
        return jsonify(error="Não logado"), 401

    dataj = request.get_json(force=True) or {}
    ws = get_sheet(ABA_LANCAMENTOS)
    ws.append_row([
        user,
        dataj.get("data"),
        dataj.get("tipo"),
        dataj.get("categoria"),
        dataj.get("descricao"),
        dataj.get("valor"),
        _utc_iso()
    ], value_input_option="USER_ENTERED")
    return jsonify(ok=True)


@app.put("/api/lancamentos/<int:row>")
def editar_lancamento(row):
    user = require_login()
    if not user:
        return jsonify(error="Não logado"), 401

    dataj = request.get_json(force=True) or {}
    ws = get_sheet(ABA_LANCAMENTOS)

    if str(ws.cell(row, 1).value).lower().strip() != user:
        return jsonify(error="Sem permissão"), 403

    ws.update(f"A{row}:G{row}", [[
        user,
        dataj.get("data"),
        dataj.get("tipo"),
        dataj.get("categoria"),
        dataj.get("descricao"),
        dataj.get("valor"),
        _utc_iso()
    ]])
    return jsonify(ok=True)


@app.delete("/api/lancamentos/<int:row>")
def deletar_lancamento(row):
    user = require_login()
    if not user:
        return jsonify(error="Não logado"), 401

    ws = get_sheet(ABA_LANCAMENTOS)
    if str(ws.cell(row, 1).value).lower().strip() != user:
        return jsonify(error="Sem permissão"), 403

    ws.delete_rows(row)
    return jsonify(ok=True)


@app.get("/api/dashboard")
def dashboard():
    user = require_login()
    if not user:
        return jsonify(error="Não logado"), 401

    mes = int(request.args.get("mes"))
    ano = int(request.args.get("ano"))

    ws = get_sheet(ABA_LANCAMENTOS)
    rows = ws.get_all_records()

    receitas = 0.0
    gastos = 0.0

    for r in rows:
        if str(r.get("user_email", "")).lower().strip() != user:
            continue
        dt = r.get("data")
        if not dt:
            continue
        try:
            d = datetime.fromisoformat(str(dt))
        except:
            continue

        if d.month == mes and d.year == ano:
            valor = parse_money(r.get("valor"))
            if str(r.get("tipo", "")).upper() == "RECEITA":
                receitas += valor
            elif str(r.get("tipo", "")).upper() == "GASTO":
                gastos += valor

    return jsonify(receitas=receitas, gastos=gastos, saldo=receitas - gastos)


# Healthcheck simples
@app.get("/health")
def health():
    return jsonify(ok=True, time=_utc_iso()), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
