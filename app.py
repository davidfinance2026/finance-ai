import os
import json
import gspread
from flask import Flask, request, jsonify, render_template, session
from google.oauth2.service_account import Credentials
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

# =========================
# CONFIGURAÇÕES FLASK
# =========================

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "super-secret-key")

# =========================
# GOOGLE SHEETS
# =========================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_gspread_client():
    creds_json = os.environ.get("SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise Exception("SERVICE_ACCOUNT_JSON não configurado")

    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)

# =========================
# PLANILHA POR USUÁRIO
# =========================

def get_or_create_user_sheet(email):
    client = get_gspread_client()

    try:
        sheet = client.open(f"FinanceAI_{email}")
    except:
        sheet = client.create(f"FinanceAI_{email}")
        sheet.sheet1.append_row(["Tipo", "Categoria", "Descrição", "Valor", "Data"])

    return sheet.sheet1

# =========================
# USUÁRIOS EM MEMÓRIA
# =========================

users = {}

# =========================
# ROTAS
# =========================

@app.route("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/register")
def register():
    data = request.json
    email = data.get("email")
    senha = data.get("senha")

    if email in users:
        return jsonify({"erro": "Usuário já existe"}), 400

    users[email] = generate_password_hash(senha)
    return jsonify({"msg": "Usuário criado com sucesso"})


@app.post("/login")
def login():
    data = request.json
    email = data.get("email")
    senha = data.get("senha")

    if email not in users:
        return jsonify({"erro": "Usuário não encontrado"}), 400

    if not check_password_hash(users[email], senha):
        return jsonify({"erro": "Senha incorreta"}), 400

    session["user"] = email
    return jsonify({"msg": "Login realizado"})


@app.post("/logout")
def logout():
    session.pop("user", None)
    return jsonify({"msg": "Logout realizado"})


@app.post("/lancar")
def lancar():
    if "user" not in session:
        return jsonify({"erro": "Faça login"}), 401

    data = request.json
    tipo = data.get("tipo")
    categoria = data.get("categoria")
    descricao = data.get("descricao")
    valor = data.get("valor")
    data_lanc = data.get("data")

    # Corrige valor brasileiro
    valor = float(str(valor).replace(".", "").replace(",", "."))

    sheet = get_or_create_user_sheet(session["user"])
    sheet.append_row([tipo, categoria, descricao, valor, data_lanc])

    return jsonify({"msg": "Lançamento salvo"})


@app.get("/ultimos")
def ultimos():
    if "user" not in session:
        return jsonify({"erro": "Faça login"}), 401

    sheet = get_or_create_user_sheet(session["user"])
    registros = sheet.get_all_records()

    return jsonify(registros[-10:])


# =========================
# MAIN
# =========================

if __name__ == "__main__":
    app.run(debug=True)
