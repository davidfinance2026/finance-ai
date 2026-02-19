from flask import Flask, request, jsonify, render_template
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)


# =============================
# GOOGLE SHEETS
# =============================
def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_file(
        "google_creds.json",
        scopes=scopes
    )

    gc = gspread.authorize(creds)
    return gc.open("Controle Financeiro").sheet1


# =============================
# HOME
# =============================
@app.route("/")
def home():
    return render_template("index.html")


# =============================
# LANÇAR
# =============================
@app.route("/lancar", methods=["POST"])
def lancar():
    body = request.get_json(force=True)

    tipo = body.get("tipo")
    categoria = body.get("categoria")
    descricao = body.get("descricao")
    valor = body.get("valor")
    data = body.get("data")

    if not data:
        data = datetime.now().strftime("%d/%m/%Y")

    sh = get_sheet()
    sh.append_row([
        data,
        tipo,
        categoria,
        descricao,
        valor
    ])

    return jsonify({"ok": True, "msg": "Lançamento salvo!"})


# =============================
# ÚLTIMOS
# =============================
@app.route("/ultimos")
def ultimos():
    sh = get_sheet()
    dados = sh.get_all_records()
    return jsonify(dados[-10:])


# =============================
# RESUMO
# =============================
@app.route("/resumo")
def resumo():
    hoje = datetime.now()
    mes_atual = hoje.month
    ano_atual = hoje.year

    sh = get_sheet()
    dados = sh.get_all_records()

    entradas = 0
    saidas = 0
    por_dia = {}

    for r in dados:
        try:
            data_txt = r.get("Data") or r.get("data")
            if not data_txt:
                continue

            d = datetime.strptime(data_txt, "%d/%m/%Y")

            if d.month == mes_atual and d.year == ano_atual:
                tipo = (r.get("Tipo") or r.get("tipo") or "").lower()
                valor = float(r.get("Valor") or r.get("valor") or 0)

                dia = d.strftime("%d")

                if dia not in por_dia:
                    por_dia[dia] = {"receita": 0, "gasto": 0}

                if "rece" in tipo:
                    entradas += valor
                    por_dia[dia]["receita"] += valor
                else:
                    saidas += valor
                    por_dia[dia]["gasto"] += valor

        except:
            continue

    saldo = entradas - saidas

    dias = sorted(por_dia.keys(), key=lambda x: int(x))
    serie_receita = [por_dia[d]["receita"] for d in dias]
    serie_gasto = [por_dia[d]["gasto"] for d in dias]

    return jsonify({
        "entradas": entradas,
        "saidas": saidas,
        "saldo": saldo,
        "dias": dias,
        "serie_receita": serie_receita,
        "serie_gasto": serie_gasto
    })


# =============================
# RUN LOCAL
# =============================
if __name__ == "__main__":
    app.run(debug=True)
