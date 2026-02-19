from datetime import datetime
from flask import Flask, request, jsonify
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file("google_creds.json", scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open("Controle Financeiro").sheet1

@app.get("/")
def home():
    return "Finance AI online!"

@app.post("/lancar")
def lancar():
    body = request.get_json(force=True)
    tipo = body.get("tipo")
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
   
