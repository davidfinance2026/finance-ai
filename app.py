import os
import json
import re
import hmac
import hashlib
import time
import requests
import gspread
from flask import Flask, render_template, request, jsonify, session, send_from_directory
from google.oauth2.service_account import Credentials
from datetime import datetime, date

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "financeai-secret")
app.config["JSON_AS_ASCII"] = False

# =========================
# Headers / Cache
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
# Google Sheets
# =========================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_NAME = "Controle Financeiro"
ABA_USUARIOS = "Usuarios"
ABA_LANCAMENTOS = "Lancamentos"
ABA_WHATSAPP = "WhatsApp"

_client = None

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
    return get_client().open(SHEET_NAME)

def get_sheet(nome_aba):
    sh = open_spreadsheet()
    return sh.worksheet(nome_aba)

def get_or_create_sheet(nome_aba, headers):
    sh = open_spreadsheet()
    try:
        ws = sh.worksheet(nome_aba)
    except Exception:
        ws = sh.add_worksheet(title=nome_aba, rows=2000, cols=len(headers) + 5)
        ws.update(f"A1:{chr(64+len(headers))}1", [headers])
        return ws

    cur = ws.row_values(1)
    if cur != headers:
        ws.update(f"A1:{chr(64+len(headers))}1", [headers])
    return ws

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

def ensure_headers():
    get_or_create_sheet(ABA_USUARIOS, ["email","senha","nome_apelido","nome_completo","telefone","criado_em"])
    get_or_create_sheet(ABA_LANCAMENTOS, ["user_email","data","tipo","categoria","descricao","valor","criado_em"])
    get_or_create_sheet(ABA_WHATSAPP, ["wa_number","user_email","criado_em"])

# =========================
# Auth Web App
# =========================
def require_login():
    return session.get("user")

# =========================
# WhatsApp Link Helpers
# =========================
def normalize_wa_number(raw) -> str:
    # raw pode vir como int, então força str
    s = str(raw or "").strip().replace("+", "")
    s = re.sub(r"[^0-9]", "", s)
    return s

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
    ws.append_row([wa_number, email, datetime.utcnow().isoformat()])
    return True, f"✅ Número conectado ao email {email}."

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
# WhatsApp Cloud API
# =========================
WA_VERIFY_TOKEN = os.environ.get("WA_VERIFY_TOKEN", "")
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_ACCESS_TOKEN = os.environ.get("WA_ACCESS_TOKEN", "")

# PROTEÇÃO #1: assinatura do webhook (habilita só se setar META_APP_SECRET)
META_APP_SECRET = os.environ.get("META_APP_SECRET", "").strip()

def verify_meta_signature(req) -> bool:
    """
    Meta envia header: X-Hub-Signature-256: sha256=<hash>
    hash = HMAC_SHA256(app_secret, raw_body)
    """
    if not META_APP_SECRET:
        return True  # não quebra o webhook se você não configurou
    sig = req.headers.get("X-Hub-Signature-256", "") or ""
    if not sig.startswith("sha256="):
        return False
    got = sig.split("=", 1)[1].strip()
    expected = hmac.new(
        META_APP_SECRET.encode("utf-8"),
        req.get_data() or b"",
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(got, expected)

def wa_send_text(to_number: str, text: str):
    if not (WA_PHONE_NUMBER_ID and WA_ACCESS_TOKEN):
        print("WA creds missing. Would send:", text)
        return
    url = f"https://graph.facebook.com/v20.0/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": normalize_wa_number(to_number),
        "type": "text",
        "text": {"body": text},
    }
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    if r.status_code >= 400:
        print("WA send error:", r.status_code, r.text)

def parse_finance_command(text: str):
    t = (text or "").strip()
    t = re.sub(r"\s+", " ", t)
    low = t.lower()

    if low.startswith(("gasto ", "despesa ")):
        tipo = "GASTO"
        t2 = t.split(" ", 1)[1].strip()
    elif low.startswith(("receita ", "entrada ")):
        tipo = "RECEITA"
        t2 = t.split(" ", 1)[1].strip()
    else:
        tipo = "GASTO"
        t2 = t

    m = re.search(r"(-?\d{1,3}(?:\.\d{3})*(?:,\d{2})|-?\d+(?:[\.,]\d{2})?)", t2)
    if not m:
        return None

    valor_raw = m.group(1)
    valor = parse_money(valor_raw)

    rest = (t2[m.end():] or "").strip(" -–—")
    if not rest:
        categoria = "Geral"
        descricao = ""
    else:
        parts = rest.split(" ", 1)
        categoria = (parts[0] or "Geral").strip().title()
        descricao = parts[1].strip() if len(parts) > 1 else ""

    return {
        "tipo": tipo,
        "valor": f"{valor:.2f}",
        "categoria": categoria,
        "descricao": descricao,
        "data": date.today().isoformat(),
    }

# =========================
# PROTEÇÃO #2: Anti-duplicidade
# =========================
# guarda msg_id por um tempo (TTL) pra evitar salvar 2x quando Meta reenviar
_PROCESSED = {}  # msg_id -> expires_epoch
_PROCESSED_TTL = int(os.environ.get("WA_DEDUP_TTL_SECONDS", "3600"))  # 1h padrão

def already_processed(msg_id: str) -> bool:
    if not msg_id:
        return False
    now = int(time.time())
    # limpeza básica
    if len(_PROCESSED) > 2000:
        for k, exp in list(_PROCESSED.items()):
            if exp <= now:
                _PROCESSED.pop(k, None)

    exp = _PROCESSED.get(msg_id)
    if exp and exp > now:
        return True

    _PROCESSED[msg_id] = now + _PROCESSED_TTL
    return False

# =========================
# Webhooks WhatsApp
# =========================
@app.get("/webhooks/whatsapp")
def wa_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token and token == WA_VERIFY_TOKEN:
        return challenge or "", 200
    return "forbidden", 403

@app.post("/webhooks/whatsapp")
def wa_webhook():
    # Proteção #1 (assinatura): só valida se META_APP_SECRET existir
    if not verify_meta_signature(request):
        # não use 401 se você quiser evitar retries agressivos; mas 401/403 é OK
        return "invalid signature", 403

    payload = request.get_json(silent=True) or {}

    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {}) or {}
                for msg in (value.get("messages", []) or []):
                    msg_id = str(msg.get("id") or "")
                    if already_processed(msg_id):
                        continue  # não processa duas vezes

                    from_number = normalize_wa_number(msg.get("from"))
                    msg_type = msg.get("type")

                    if msg_type == "text":
                        body = ((msg.get("text") or {}) or {}).get("body", "") or ""
                        cmd = body.strip()
                        low = cmd.lower()

                        # fluxo simples: se mandou email puro, vincula automaticamente
                        if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", cmd):
                            ok, resp = link_wa_to_email(from_number, cmd)
                            wa_send_text(from_number, resp + "\n\nAgora envie: gasto 32,90 mercado")
                            continue

                        if low.startswith("conectar "):
                            email = cmd.split(" ", 1)[1].strip()
                            _, resp = link_wa_to_email(from_number, email)
                            wa_send_text(from_number, resp + "\n\nAgora envie: gasto 32,90 mercado")
                            continue

                        if low in ("desconectar", "desconectar whatsapp"):
                            _, resp = unlink_wa(from_number)
                            wa_send_text(from_number, resp)
                            continue

                        user_email = find_user_by_wa(from_number)
                        if not user_email:
                            wa_send_text(
                                from_number,
                                "🔒 Antes de registrar lançamentos, preciso vincular seu número.\n\n"
                                "Envie seu email do app (ex: nome@dominio.com)\n"
                                "ou: conectar SEU_EMAIL_DO_APP"
                            )
                            continue

                        parsed = parse_finance_command(cmd)
                        if not parsed:
                            wa_send_text(
                                from_number,
                                "Não entendi 😅\n\nUse assim:\n"
                                "• gasto 32,90 mercado\n"
                                "• receita 2500 salario\n"
                                "• 32,90 mercado (assume gasto)"
                            )
                            continue

                        ensure_headers()
                        ws_lanc = get_sheet(ABA_LANCAMENTOS)
                        ws_lanc.append_row([
                            user_email,
                            parsed["data"],
                            parsed["tipo"],
                            parsed["categoria"],
                            parsed["descricao"],
                            parsed["valor"],
                            datetime.utcnow().isoformat()
                        ])

                        wa_send_text(
                            from_number,
                            "✅ Lançamento salvo!\n"
                            f"Tipo: {parsed['tipo']}\n"
                            f"Valor: R$ {parsed['valor'].replace('.', ',')}\n"
                            f"Categoria: {parsed['categoria']}\n"
                            f"Data: {parsed['data']}"
                        )
                        continue

                    if msg_type in ("image", "document", "audio", "video"):
                        user_email = find_user_by_wa(from_number)
                        if not user_email:
                            wa_send_text(from_number, "🔒 Conecte primeiro: envie seu email ou 'conectar SEU_EMAIL_DO_APP'")
                            continue

                        media = msg.get(msg_type, {}) or {}
                        media_id = media.get("id")
                        caption = (media.get("caption") or "").strip()

                        ensure_headers()
                        ws_lanc = get_sheet(ABA_LANCAMENTOS)
                        ws_lanc.append_row([
                            user_email,
                            date.today().isoformat(),
                            "GASTO",
                            "Comprovante",
                            f"{caption} [MID:{media_id}]".strip(),
                            "0.00",
                            datetime.utcnow().isoformat()
                        ])

                        wa_send_text(
                            from_number,
                            "📎 Comprovante recebido!\n"
                            "Salvei como 'Comprovante' (valor 0,00) para você editar depois no app."
                        )
                        continue

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
    email = str(data.get("email","")).lower().strip()
    senha = str(data.get("senha",""))
    confirmar = str(data.get("confirmar_senha",""))

    nome_apelido = str(data.get("nome_apelido",""))
    nome_completo = str(data.get("nome_completo",""))
    telefone = str(data.get("telefone",""))

    if not email or not senha:
        return jsonify(error="Email e senha obrigatórios"), 400
    if senha != confirmar:
        return jsonify(error="Senhas não conferem"), 400

    ws = get_sheet(ABA_USUARIOS)
    emails = [e.lower().strip() for e in ws.col_values(1)]
    if email in emails:
        return jsonify(error="Email já cadastrado"), 400

    ws.append_row([email, senha, nome_apelido, nome_completo, telefone, datetime.utcnow().isoformat()])
    session["user"] = email
    return jsonify(email=email)

@app.post("/api/login")
def login():
    ensure_headers()
    data = request.get_json(force=True) or {}
    email = str(data.get("email","")).lower().strip()
    senha = str(data.get("senha",""))

    ws = get_sheet(ABA_USUARIOS)
    rows = ws.get_all_records()
    for r in rows:
        if str(r.get("email","")).lower().strip() == email and str(r.get("senha","")) == senha:
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
    email = str(data.get("email","")).lower().strip()
    nova = str(data.get("nova_senha",""))
    conf = str(data.get("confirmar",""))

    if not email or not nova:
        return jsonify(error="Email e nova senha obrigatórios"), 400
    if nova != conf:
        return jsonify(error="Senhas não conferem"), 400

    ws = get_sheet(ABA_USUARIOS)
    rows = ws.get_all_records()
    for i, r in enumerate(rows, start=2):
        if str(r.get("email","")).lower().strip() == email:
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
        if str(r.get("user_email","")).lower().strip() == user:
            r["row"] = idx
            items.append(r)
    items.sort(key=lambda x: x.get("data",""), reverse=True)
    return jsonify(items=items[:limit])

@app.post("/api/lancamentos")
def criar_lancamento():
    user = require_login()
    if not user:
        return jsonify(error="Não logado"), 401
    data = request.get_json(force=True) or {}
    ws = get_sheet(ABA_LANCAMENTOS)
    ws.append_row([
        user,
        data.get("data"),
        data.get("tipo"),
        data.get("categoria"),
        data.get("descricao"),
        data.get("valor"),
        datetime.utcnow().isoformat()
    ])
    return jsonify(ok=True)

@app.put("/api/lancamentos/<int:row>")
def editar_lancamento(row):
    user = require_login()
    if not user:
        return jsonify(error="Não logado"), 401
    data = request.get_json(force=True) or {}
    ws = get_sheet(ABA_LANCAMENTOS)
    if str(ws.cell(row,1).value).lower().strip() != user:
        return jsonify(error="Sem permissão"), 403
    ws.update(f"A{row}:G{row}", [[
        user,
        data.get("data"),
        data.get("tipo"),
        data.get("categoria"),
        data.get("descricao"),
        data.get("valor"),
        datetime.utcnow().isoformat()
    ]])
    return jsonify(ok=True)

@app.delete("/api/lancamentos/<int:row>")
def deletar_lancamento(row):
    user = require_login()
    if not user:
        return jsonify(error="Não logado"), 401
    ws = get_sheet(ABA_LANCAMENTOS)
    if str(ws.cell(row,1).value).lower().strip() != user:
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
        if str(r.get("user_email","")).lower().strip() != user:
            continue
        dt = r.get("data")
        if not dt:
            continue
        try:
            d = datetime.fromisoformat(dt)
        except:
            continue
        if d.month == mes and d.year == ano:
            valor = parse_money(r.get("valor"))
            if str(r.get("tipo","")).upper() == "RECEITA":
                receitas += valor
            elif str(r.get("tipo","")).upper() == "GASTO":
                gastos += valor
    return jsonify(receitas=receitas, gastos=gastos, saldo=receitas-gastos)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
