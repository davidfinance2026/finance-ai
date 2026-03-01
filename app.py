import os
import json
import re
import requests
import gspread
from flask import Flask, render_template, request, jsonify, session, send_from_directory
from google.oauth2.service_account import Credentials
from datetime import datetime, date

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "financeai-secret")
app.config["JSON_AS_ASCII"] = False

# =========================
# Response headers
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

# Sua planilha e abas
SHEET_NAME = "Controle Financeiro"
ABA_USUARIOS = "Usuarios"
ABA_LANCAMENTOS = "Lancamentos"
ABA_WHATSAPP = "WhatsApp"
ABA_METAS = "Metas"
ABA_INVESTIMENTOS = "Investimentos"
ABA_PASSWORD_RESETS = "PassworResets"  # (mantive como você escreveu)

# Preferível (Railway): abrir por ID
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "") or os.environ.get("SPREADSHEET_ID".lower(), "")
# (se você estiver usando a variável SPREADSHEET_ID no Railway, ok)
# Se por acaso você ainda estiver com SPREADSHEET_ID no Railway, deixe assim:
# SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")

DEBUG_LOG_PAYLOAD = os.environ.get("DEBUG_LOG_PAYLOAD", "false").lower() == "true"

_client = None
_spreadsheet = None

def now_utc_iso():
    return datetime.utcnow().isoformat()

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
    """
    Recomendado: open_by_key(SPREADSHEET_ID)
    Fallback: open por nome (SHEET_NAME)
    """
    global _spreadsheet
    if _spreadsheet:
        return _spreadsheet

    cli = get_client()
    if SPREADSHEET_ID:
        _spreadsheet = cli.open_by_key(SPREADSHEET_ID)
    else:
        # fallback se você não quiser usar ID
        _spreadsheet = cli.open(SHEET_NAME)

    return _spreadsheet

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

def ensure_headers():
    # mantém suas colunas
    get_or_create_sheet(ABA_USUARIOS, ["email","senha","nome_apelido","nome_completo","telefone","criado_em"])
    get_or_create_sheet(ABA_LANCAMENTOS, ["user_email","data","tipo","categoria","descricao","valor","criado_em"])
    get_or_create_sheet(ABA_WHATSAPP, ["wa_number","user_email","criado_em"])

    # (opcional) se quiser garantir também as outras abas
    # get_or_create_sheet(ABA_METAS, ["user_email","titulo","valor_alvo","valor_atual","data_limite","criado_em"])
    # get_or_create_sheet(ABA_INVESTIMENTOS, ["user_email","ativo","tipo","quantidade","preco","criado_em"])
    # get_or_create_sheet(ABA_PASSWORD_RESETS, ["email","codeHash","expiresAt","createdAt","usedAt"])

def require_login():
    return session.get("user")

def normalize_wa_number(raw: str) -> str:
    s = (raw or "").strip().replace("+", "")
    s = re.sub(r"[^0-9]", "", s)
    return s

EMAIL_RE = re.compile(r"^[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$", re.IGNORECASE)

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
    """
    ✅ Aqui é onde o WhatsApp PRECISA registrar.
    Vai atualizar se já existir, ou inserir nova linha na aba WhatsApp.
    """
    ensure_headers()
    ws = get_sheet(ABA_WHATSAPP)
    wa_number = normalize_wa_number(wa_number)
    email = (email or "").lower().strip()

    if not wa_number or not email:
        return False, "Número e email são obrigatórios."
    if not EMAIL_RE.match(email):
        return False, "Esse texto não parece um e-mail válido."

    rows = ws.get_all_records()
    for i, r in enumerate(rows, start=2):
        if normalize_wa_number(r.get("wa_number")) == wa_number:
            ws.update_cell(i, 2, email)
            ws.update_cell(i, 3, now_utc_iso())
            return True, f"✅ Número atualizado para {email}."

    ws.append_row([wa_number, email, now_utc_iso()])
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

def parse_finance_command(text: str):
    """
    Aceita:
      - "gasto 32,90 mercado"
      - "receita 2500 salario"
      - "32,90 mercado" (assume gasto)
    """
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

    m = re.search(r"(-?\d{1,3}(?:\.\d{3})*(?:,\d{1,2})|-?\d+(?:[\.,]\d{1,2})?)", t2)
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
# WhatsApp Cloud API
# =========================
WA_VERIFY_TOKEN = os.environ.get("WA_VERIFY_TOKEN", "")
WA_PHONE_NUMBER_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
WA_ACCESS_TOKEN = os.environ.get("WA_ACCESS_TOKEN", "")
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v22.0")

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
    payload = request.get_json(silent=True) or {}

    if DEBUG_LOG_PAYLOAD:
        print("======= INCOMING WEBHOOK =======")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("================================")

    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {}) or {}
                for msg in (value.get("messages", []) or []):
                    from_number = normalize_wa_number(msg.get("from"))
                    msg_type = msg.get("type")

                    if msg_type != "text":
                        # opcional: tratar mídia depois
                        wa_send_text(from_number, "📎 Por enquanto aceito apenas texto para lançamentos.")
                        continue

                    body = ((msg.get("text") or {}) or {}).get("body", "") or ""
                    cmd = body.strip()
                    low = cmd.lower()

                    # 1) comandos explícitos
                    if low.startswith("conectar "):
                        email = cmd.split(" ", 1)[1].strip()
                        ok, resp = link_wa_to_email(from_number, email)
                        wa_send_text(from_number, resp + "\n\nAgora envie: gasto 32,90 mercado")
                        continue

                    if low in ("desconectar", "desconectar whatsapp"):
                        _, resp = unlink_wa(from_number)
                        wa_send_text(from_number, resp)
                        continue

                    # 2) Se NÃO estiver vinculado, aceite email "puro" (isso resolve seu caso do print)
                    user_email = find_user_by_wa(from_number)
                    if not user_email:
                        if EMAIL_RE.match(cmd):
                            ok, resp = link_wa_to_email(from_number, cmd)
                            wa_send_text(from_number,
                                "✅ Número vinculado com sucesso!\n\n"
                                f"Email: {cmd}\n\n"
                                "Agora você pode enviar lançamentos.\n"
                                "Exemplos:\n"
                                "• gasto 35,90 mercado\n"
                                "• 120 aluguel\n"
                                "• recebi 1000 salario"
                            )
                            continue

                        wa_send_text(from_number,
                            "🔒 Antes de registrar lançamentos, preciso vincular seu número.\n\n"
                            "Por favor, me envie seu email (ex: nome@dominio.com).\n"
                            "Ou use: conectar SEU_EMAIL_DO_APP"
                        )
                        continue

                    # 3) Já vinculado -> registrar lançamento
                    parsed = parse_finance_command(cmd)
                    if not parsed:
                        wa_send_text(from_number,
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
                        now_utc_iso()
                    ])

                    wa_send_text(from_number,
                        "✅ Lançamento registrado!\n"
                        f"{parsed['tipo']} • {parsed['categoria']}\n"
                        f"{parsed['descricao']}\n"
                        f"Valor: R$ {parsed['valor'].replace('.', ',')}\n"
                        f"Data: {parsed['data']}"
                    )

    except Exception as e:
        print("WA webhook error:", repr(e))
        # não explode o webhook
        return "ok", 200

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

    ws.append_row([email, senha, nome_apelido, nome_completo, telefone, now_utc_iso()])
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
    dataj = request.get_json(force=True) or {}
    ws = get_sheet(ABA_LANCAMENTOS)
    ws.append_row([
        user,
        dataj.get("data"),
        dataj.get("tipo"),
        dataj.get("categoria"),
        dataj.get("descricao"),
        dataj.get("valor"),
        now_utc_iso()
    ])
    return jsonify(ok=True)

@app.put("/api/lancamentos/<int:row>")
def editar_lancamento(row):
    user = require_login()
    if not user:
        return jsonify(error="Não logado"), 401
    dataj = request.get_json(force=True) or {}
    ws = get_sheet(ABA_LANCAMENTOS)
    if str(ws.cell(row,1).value).lower().strip() != user:
        return jsonify(error="Sem permissão"), 403
    ws.update(f"A{row}:G{row}", [[
        user,
        dataj.get("data"),
        dataj.get("tipo"),
        dataj.get("categoria"),
        dataj.get("descricao"),
        dataj.get("valor"),
        now_utc_iso()
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
