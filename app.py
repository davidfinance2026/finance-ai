import os
import json
from datetime import datetime
from flask import Flask, request, jsonify, render_template
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = (os.getenv("SHEET_ID") or "").strip()
WORKSHEET_NAME = (os.getenv("WORKSHEET_NAME") or "Página1").strip()

def get_client():
    """
    Render-friendly: usa SERVICE_ACCOUNT_JSON (env) em vez de arquivo.
    """
    sa_env = (os.getenv("SERVICE_ACCOUNT_JSON") or "").strip()
    if not sa_env:
        raise RuntimeError(
            "SERVICE_ACCOUNT_JSON não configurado. "
            "No Render -> Environment, cole o JSON inteiro da conta de serviço."
        )

    info = json.loads(sa_env)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet():
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID não configurado no Render -> Environment.")
    client = get_client()
    sh = client.open_by_key(SHEET_ID)
    ws = sh.worksheet(WORKSHEET_NAME)
    return ws

# ------------------------
# Helpers
# ------------------------
def parse_data_br(s: str):
    return datetime.strptime(s.strip(), "%d/%m/%Y").date()

def parse_float(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    # aceita "1.234,56" ou "1234.56"
    txt = str(v).strip()
    txt = txt.replace(" ", "")
    if "," in txt and "." in txt:
        txt = txt.replace(".", "").replace(",", ".")
    else:
        txt = txt.replace(",", ".")
    try:
        return float(txt)
    except:
        return 0.0

def pick(row, *keys):
    for k in keys:
        if k in row:
            return row.get(k)
    return None

def ensure_header(ws):
    header = ws.row_values(1)
    expected = ["Data", "Tipo", "Categoria", "Descrição", "Valor"]
    if header[:5] != expected:
        ws.update("A1:E1", [expected])

# ------------------------
# Rotas
# ------------------------
@app.get("/")
def home():
    return render_template("index.html")

@app.post("/lancar")
def lancar():
    body = request.get_json(silent=True) or {}

    tipo = str(body.get("tipo", "")).strip()
    categoria = str(body.get("categoria", "")).strip()
    descricao = str(body.get("descricao", "")).strip()
    valor = parse_float(body.get("valor"))
    data = str(body.get("data", "")).strip()

    if not tipo or not categoria or not descricao:
        return jsonify({"msg": "Campos obrigatórios: tipo, categoria, descricao"}), 400
    if valor == 0:
        return jsonify({"msg": "Valor inválido"}), 400

    if not data:
        data = datetime.now().strftime("%d/%m/%Y")

    ws = get_sheet()
    ensure_header(ws)
    ws.append_row([data, tipo, categoria, descricao, valor])

    return jsonify({"ok": True})

@app.get("/ultimos")
def ultimos():
    ws = get_sheet()
    ensure_header(ws)

    dados = ws.get_all_records()  # começa da linha 2

    out = []
    for idx, r in enumerate(dados, start=2):
        out.append({
            "_row": idx,
            "Data": pick(r, "Data", "data") or "",
            "Tipo": pick(r, "Tipo", "tipo") or "",
            "Categoria": pick(r, "Categoria", "categoria") or "",
            "Descrição": pick(r, "Descrição", "Descricao", "descricao") or "",
            "Valor": pick(r, "Valor", "valor") or 0,
        })

    return jsonify(out)

@app.get("/resumo")
def resumo():
    """
    /resumo?mes=YYYY-MM  (opcional)
    """
    mes = request.args.get("mes")
    hoje = datetime.now().date()
    if not mes:
        mes = hoje.strftime("%Y-%m")

    try:
        ano, mm = mes.split("-")
        ano = int(ano)
        mm = int(mm)
    except:
        return jsonify({"msg": "mes inválido. Use YYYY-MM"}), 400

    ws = get_sheet()
    ensure_header(ws)
    dados = ws.get_all_records()

    do_mes = []
    for r in dados:
        data_txt = pick(r, "Data", "data")
        if not data_txt:
            continue

        try:
            d = parse_data_br(str(data_txt))
        except:
            continue

        if d.year == ano and d.month == mm:
            tipo_txt = str(pick(r, "Tipo", "tipo") or "").strip().lower()
            valor = parse_float(pick(r, "Valor", "valor"))
            categoria = str(pick(r, "Categoria", "categoria") or "Sem categoria")

            is_receita = "rece" in tipo_txt
            do_mes.append({
                "data": d.strftime("%d/%m/%Y"),
                "tipo": "Receita" if is_receita else "Gasto",
                "categoria": categoria,
                "valor": valor,
            })

    entradas = sum(x["valor"] for x in do_mes if x["tipo"] == "Receita")
    saidas   = sum(x["valor"] for x in do_mes if x["tipo"] == "Gasto")
    saldo    = entradas - saidas

    # série por dia (somente dias que existem)
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
    serie_gasto   = [por_dia[d]["gasto"] for d in dias]

    # pizza por categoria
    gastos_cat = {}
    receitas_cat = {}
    for x in do_mes:
        cat = x["categoria"] or "Sem categoria"
        if x["tipo"] == "Gasto":
            gastos_cat[cat] = gastos_cat.get(cat, 0.0) + x["valor"]
        else:
            receitas_cat[cat] = receitas_cat.get(cat, 0.0) + x["valor"]

    gastos_sorted = sorted(gastos_cat.items(), key=lambda kv: kv[1], reverse=True)
    receitas_sorted = sorted(receitas_cat.items(), key=lambda kv: kv[1], reverse=True)

    return jsonify({
        "mes": mes,
        "entradas": entradas,
        "saidas": saidas,
        "saldo": saldo,

        "dias": dias,
        "serie_receita": serie_receita,
        "serie_gasto": serie_gasto,

        "pizza_gastos_labels": [k for k, v in gastos_sorted],
        "pizza_gastos_values": [v for k, v in gastos_sorted],

        "pizza_receitas_labels": [k for k, v in receitas_sorted],
        "pizza_receitas_values": [v for k, v in receitas_sorted],
    })

@app.patch("/lancamento/<int:row>")
def editar(row: int):
    body = request.get_json(silent=True) or {}

    tipo = str(body.get("tipo", "")).strip()
    categoria = str(body.get("categoria", "")).strip()
    descricao = str(body.get("descricao", "")).strip()
    valor = parse_float(body.get("valor"))
    data = str(body.get("data", "")).strip()

    if not tipo or not categoria or not descricao or not data:
        return jsonify({"msg": "Campos obrigatórios: tipo, categoria, descricao, data"}), 400
    if valor == 0:
        return jsonify({"msg": "Valor inválido"}), 400

    ws = get_sheet()
    ensure_header(ws)
    ws.update(f"A{row}:E{row}", [[data, tipo, categoria, descricao, valor]])

    return jsonify({"ok": True})

@app.delete("/lancamento/<int:row>")
def deletar(row: int):
    ws = get_sheet()
    ws.delete_rows(row)
    return jsonify({"ok": True})
