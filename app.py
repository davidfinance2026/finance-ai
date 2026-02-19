import os
import json
from datetime import datetime  # <-- garante datetime
from flask import Flask, request, jsonify, render_template
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)  # por padrão usa template_folder="templates"

# =========================
# Google Sheets
# =========================
def get_sheet():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise Exception("Variável GOOGLE_CREDENTIALS não encontrada no Render")

    creds_dict = json.loads(creds_json)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    # Nome da planilha (pode deixar fixo ou colocar em variável no Render)
    sheet_name = os.environ.get("SHEET_NAME", "FinanceAI")
    sh = client.open(sheet_name).sheet1
    return sh


# =========================
# Helpers
# =========================
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

def ensure_header(sheet):
    """
    Garante que a primeira linha (cabeçalho) existe.
    """
    try:
        values = sheet.row_values(1)
        if not values or values[:5] != ["Tipo", "Categoria", "Descrição", "Valor", "Data"]:
            sheet.insert_row(["Tipo", "Categoria", "Descrição", "Valor", "Data"], 1)
    except:
        pass


# =========================
# Rotas
# =========================
@app.get("/")
def home():
    # Se não existir templates/index.html, não quebra o Render.
    try:
        return render_template("index.html")
    except Exception:
        return """
        <h2>Finance AI</h2>
        <p>Faltou criar o arquivo <b>templates/index.html</b>.</p>
        <p>Crie a pasta <b>templates</b> e coloque seu HTML dentro de <b>index.html</b>.</p>
        """, 200


@app.get("/health")
def health():
    return "ok", 200


@app.post("/lancar")
def lancar():
    payload = request.get_json(silent=True) or {}

    tipo = (payload.get("tipo") or "").strip()
    categoria = (payload.get("categoria") or "").strip()
    descricao = (payload.get("descricao") or "").strip()
    valor = payload.get("valor")
    data_txt = (payload.get("data") or "").strip()

    if not tipo or not categoria or not descricao or valor is None:
        return jsonify({"msg": "Preencha tipo, categoria, descrição e valor."}), 400

    # Data opcional (se vier vazia, usa hoje)
    if not data_txt:
        data_txt = datetime.now().strftime("%d/%m/%Y")

    sheet = get_sheet()
    ensure_header(sheet)

    sheet.append_row([tipo, categoria, descricao, valor, data_txt])
    return jsonify({"msg": "✅ Lançamento salvo!"}), 200


@app.get("/ultimos")
def ultimos():
    sheet = get_sheet()
    ensure_header(sheet)
    dados = sheet.get_all_records()
    return jsonify(dados[-10:]), 200


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
    ensure_header(sheet)
    dados = sheet.get_all_records()

    do_mes = []
    for r in dados:
        data_txt = r.get("Data")
        if not data_txt:
            continue

        try:
            d = parse_data_br(str(data_txt))
        except:
            continue

        if d.year == ano and d.month == mm:
            tipo_raw = str(r.get("Tipo") or "").strip().lower()
            valor = parse_float(r.get("Valor"))
            do_mes.append({
                "data": d.strftime("%d/%m/%Y"),
                "tipo": "Receita" if "rece" in tipo_raw else "Gasto",
                "categoria": str(r.get("Categoria") or ""),
                "descricao": str(r.get("Descrição") or ""),
                "valor": valor,
            })

    entradas = sum(x["valor"] for x in do_mes if x["tipo"] == "Receita")
    saidas   = sum(x["valor"] for x in do_mes if x["tipo"] == "Gasto")
    saldo    = entradas - saidas

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
    serie_gasto   = [por_dia[d]["gasto"]   for d in dias]

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
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=True)
