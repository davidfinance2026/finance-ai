import os
import json
import gspread
from flask import Flask, render_template, request, jsonify, session
from google.oauth2.service_account import Credentials
from datetime import datetime

# -------------------------------
# Flask setup
# -------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "financeai-secret")

# FORCE UTF‑8 (fix encoding problems)
@app.after_request
def force_utf8(response):
    ct = response.headers.get("Content-Type", "")
    if "charset" not in ct.lower():
        if ct.startswith("text/") or "json" in ct or "javascript" in ct:
            response.headers["Content-Type"] = f"{ct}; charset=utf-8"
    return response

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

# -------------------------------
# Routes
# -------------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/register", methods=["POST"])
def register():
    ensure_headers()
    data = request.json
    email = data.get("email","").lower().strip()
    senha = data.get("senha","")
    confirmar = data.get("confirmar_senha","")
    nome_apelido = data.get("nome_apelido","")
    nome_completo = data.get("nome_completo","")
    telefone = data.get("telefone","")

    if not email or not senha:
        return jsonify(error="Email e senha obrigatórios"), 400
    if senha != confirmar:
        return jsonify(error="Senhas não conferem"), 400

    ws = get_sheet(ABA_USUARIOS)
    emails = ws.col_values(1)
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

@app.route("/api/login", methods=["POST"])
def login():
    ensure_headers()
    data = request.json
    email = data.get("email","").lower().strip()
    senha = data.get("senha","")

    ws = get_sheet(ABA_USUARIOS)
    rows = ws.get_all_records()

    for r in rows:
        if r["email"].lower() == email and r["senha"] == senha:
            session["user"] = email
            return jsonify(email=email)

    return jsonify(error="Email ou senha inválidos"), 401

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify(ok=True)

@app.route("/api/reset_password", methods=["POST"])
def reset_password():
    ensure_headers()
    data = request.json
    email = data.get("email","").lower().strip()
    nova = data.get("nova_senha","")
    conf = data.get("confirmar","")

    if nova != conf:
        return jsonify(error="Senhas não conferem"), 400

    ws = get_sheet(ABA_USUARIOS)
    rows = ws.get_all_records()
    for i, r in enumerate(rows, start=2):
        if r["email"].lower() == email:
            ws.update_cell(i, 2, nova)
            return jsonify(ok=True)

    return jsonify(error="Email não encontrado"), 404

# -------------------------------
# Lancamentos
# -------------------------------
@app.route("/api/lancamentos", methods=["GET"])
def listar_lancamentos():
    user = session.get("user")
    if not user:
        return jsonify(error="Não logado"), 401

    limit = int(request.args.get("limit", 50))

    ws = get_sheet(ABA_LANCAMENTOS)
    rows = ws.get_all_records()

    items = []
    for idx, r in enumerate(rows, start=2):
        if r["user_email"] == user:
            r["row"] = idx
            items.append(r)

    items.sort(key=lambda x: x.get("data",""), reverse=True)
    return jsonify(items=items[:limit])

@app.route("/api/lancamentos", methods=["POST"])
def criar_lancamento():
    user = session.get("user")
    if not user:
        return jsonify(error="Não logado"), 401

    data = request.json

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

@app.route("/api/lancamentos/<int:row>", methods=["PUT"])
def editar_lancamento(row):
    user = session.get("user")
    if not user:
        return jsonify(error="Não logado"), 401

    data = request.json
    ws = get_sheet(ABA_LANCAMENTOS)

    if ws.cell(row,1).value != user:
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

@app.route("/api/lancamentos/<int:row>", methods=["DELETE"])
def deletar_lancamento(row):
    user = session.get("user")
    if not user:
        return jsonify(error="Não logado"), 401

    ws = get_sheet(ABA_LANCAMENTOS)
    if ws.cell(row,1).value != user:
        return jsonify(error="Sem permissão"), 403

    ws.delete_rows(row)
    return jsonify(ok=True)

# -------------------------------
# Dashboard
# -------------------------------
@app.route("/api/dashboard")
def dashboard():
    user = session.get("user")
    if not user:
        return jsonify(error="Não logado"), 401

    mes = int(request.args.get("mes"))
    ano = int(request.args.get("ano"))

    ws = get_sheet(ABA_LANCAMENTOS)
    rows = ws.get_all_records()

    receitas = 0
    gastos = 0

    for r in rows:
        if r["user_email"] != user:
            continue

        if not r["data"]:
            continue

        d = datetime.fromisoformat(r["data"])
        if d.month == mes and d.year == ano:
            valor = parse_money(r["valor"])
            if r["tipo"] == "RECEITA":
                receitas += valor
            elif r["tipo"] == "GASTO":
                gastos += valor

    saldo = receitas - gastos

    return jsonify(
        receitas=receitas,
        gastos=gastos,
        saldo=saldo
    )

# -------------------------------
# Run
# -------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
