import os
import json
import gspread
from flask import Flask, render_template, request, jsonify, session, send_from_directory
from google.oauth2.service_account import Credentials
from datetime import datetime

# -------------------------------
# Flask setup
# -------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "financeai-secret")

# JSON in UTF-8 (do not escape accents)
app.config["JSON_AS_ASCII"] = False

# FORCE UTF-8 ALWAYS (override any wrong charset)
@app.after_request
def force_utf8(response):
    mt = (response.mimetype or "").lower()

    if mt in ("text/html", "text/plain", "text/css", "application/javascript", "text/javascript"):
        response.headers["Content-Type"] = f"{mt}; charset=utf-8"
    elif mt == "application/json":
        response.headers["Content-Type"] = "application/json; charset=utf-8"
    else:
        # for safety, if it's text/* but unknown
        if mt.startswith("text/"):
            response.headers["Content-Type"] = f"{mt}; charset=utf-8"

    return response

# Serve robots.txt at /robots.txt (standard)
@app.get("/robots.txt")
def robots_txt():
    return send_from_directory("static", "robots.txt")

# -------------------------------
# Google Sheets setup
# -------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_NAME = "Controle Financeiro"
ABA_USUARIOS = "Usuarios"
ABA_LANCAMENTOS = "Lancamentos"

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

def get_sheet(nome_aba):
    client = get_client()
    sh = client.open(SHEET_NAME)
    return sh.worksheet(nome_aba)

# -------------------------------
# Helpers
# -------------------------------
def parse_money(v):
    if v is None:
        return 0.0
    s = str(v).strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except:
        return 0.0

def ensure_headers():
    ws = get_sheet(ABA_USUARIOS)
    headers = ws.row_values(1)
    expected = ["email","senha","nome_apelido","nome_completo","telefone","criado_em"]
    if headers != expected:
        ws.update("A1:F1", [expected])

    ws2 = get_sheet(ABA_LANCAMENTOS)
    headers2 = ws2.row_values(1)
    expected2 = ["user_email","data","tipo","categoria","descricao","valor","criado_em"]
    if headers2 != expected2:
        ws2.update("A1:G1", [expected2])

def require_login():
    return session.get("user")

# -------------------------------
# Routes
# -------------------------------
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

    ws.append_row([
        email,
        senha,
        nome_apelido,
        nome_completo,
        telefone,
        datetime.utcnow().isoformat()
    ])

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

# -------------------------------
# Lancamentos
# -------------------------------
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

# -------------------------------
# Dashboard
# -------------------------------
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
            d = datetime.fromisoformat(dt)  # expects YYYY-MM-DD
        except:
            continue

        if d.month == mes and d.year == ano:
            valor = parse_money(r.get("valor"))
            if str(r.get("tipo","")).upper() == "RECEITA":
                receitas += valor
            elif str(r.get("tipo","")).upper() == "GASTO":
                gastos += valor

    saldo = receitas - gastos
    return jsonify(receitas=receitas, gastos=gastos, saldo=saldo)

# -------------------------------
# Run
# -------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
