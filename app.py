import os
import json
from datetime import datetime
from flask import Flask, request, jsonify, render_template
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ==============================
# CONFIG GOOGLE SHEETS
# ==============================

def get_sheet():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")

    if not creds_json:
        raise Exception("Variável GOOGLE_CREDENTIALS não encontrada")

    creds_dict = json.loads(creds_json)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    # Nome da sua planilha
    sheet = client.open("FinanceAI").sheet1
    return sheet


# ==============================
# FUNÇÕES AUXILIARES
# ==============================

def parse_data_br(data_str):
    return datetime.strptime(data_str, "%d/%m/%Y").date()

def parse_float(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    v = str(v).strip().replace(".", "").replace(",", ".")
    try:
        return float(v)
    except:
        return 0.0


# ==============================
# ROTAS
# ==============================

@app.get("/")
def home():
    return render_template("index.html")


@app.post("/lancar")
def lancar():
    data = request.json

    tipo = data.get("tipo")
    categoria = data.get("categoria")
    descricao = data.get("descricao")
    valor = data.get("valor")
    data_txt = data.get("data")

    if not tipo or not categoria or not descricao or not valor:
        return jsonify({"msg": "Dados incompletos"}), 400

    if not data_txt:
        hoje = datetime.now().strftime("%d/%m/%Y")
        data_txt = hoje

    sheet = get_sheet()

    sheet.append_row([
        tipo,
        categoria,
        descricao,
        valor,
        data_txt
    ])

    return jsonify({"msg": "Salvo com sucesso"})


@app.get("/ultimos")
def ultimos():
    sheet = get_sheet()
    dados = sheet.get_all_records()
    return jsonify(dados[-10:])


@app.get("/resumo")
def resumo():
    mes = request.args.get("mes")
    hoje = datetime.now().date()

    if not mes:
        mes = hoje.strftime("%Y-%m")

    ano, mm = mes.split("-")
    ano = int(ano)
    mm = int(mm)

    sheet = get_sheet()
    dados = sheet.get_all_records()

    do_mes = []

    for r in dados:
        try:
            d = parse_data_br(str(r.get("Data")))
        except:
            continue

        if d.year == ano and d.month == mm:
            tipo = str(r.get("Tipo")).strip().lower()
            valor = parse_float(r.get("Valor"))

            do_mes.append({
                "data": d.strftime("%d/%m/%Y"),
                "tipo": "Receita" if "rece" in tipo else "Gasto",
                "categoria": r.get("Categoria"),
                "descricao": r.get("Descrição"),
                "valor": valor
            })

    entradas = sum(x["valor"] for x in do_mes if x["tipo"] == "Receita")
    saidas = sum(x["valor"] for x in do_mes if x["tipo"] == "Gasto")
    saldo = entradas - saidas

    por_dia = {}

    for x in do_mes:
        dia = x["data"][:2]
        por_dia.setdefault(dia, {"receita": 0.0, "gasto": 0.0})

        if x["tipo"] == "Receita":
            por_dia[dia]["receita"] += x["valor"]
        else:
            por_dia[dia]["gasto"] += x["valor"]

    dias = sorted(por_dia.keys(), key=lambda z: int(z))
    serie_receita = [por_dia[d]["receita"] for d in dias]
    serie_gasto = [por_dia[d]["gasto"] for d in dias]

    return jsonify({
        "mes": mes,
        "entradas": entradas,
        "saidas": saidas,
        "saldo": saldo,
        "dias": dias,
        "serie_receita": serie_receita,
        "serie_gasto": serie_gasto,
        "ultimos": do_mes[-10:],
        "qtd": len(do_mes)
    })


# ==============================
# START (para rodar local)
# ==============================

if __name__ == "__main__":
    app.run(debug=True)
