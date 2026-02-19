import os, json
from datetime import datetime
from flask import Flask, request, jsonify
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

def get_sheet():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    if not creds_json:
        raise RuntimeError("Faltou configurar GOOGLE_CREDS_JSON no Render.")

    creds_dict = json.loads(creds_json)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)

    # Nome exato da sua planilha no Google Drive:
    return gc.open("Controle Financeiro").sheet1

@app.get("/")
def home():
    return "Finance AI online!"

@app.post("/lancar")
def lancar():
    body = request.get_json(force=True)

    tipo = body.get("tipo")          # "Receita" ou "Gasto"
    categoria = body.get("categoria")
    descricao = body.get("descricao")
    valor = body.get("valor")

    sh = get_sheet()
    sh.append_row([
        datetime.now().strftime("%d/%m/%Y"),
        tipo, categoria, descricao, valor
    ])

    return jsonify({"ok": True, "msg": "Lançamento salvo!"})

@app.get("/ultimos")
def ultimos():
    sh = get_sheet()
    dados = sh.get_all_records()
    return jsonify(dados[-10:])

# NÃO use app.run() em produção no Render.
# Gunicorn é quem roda o servidor.
