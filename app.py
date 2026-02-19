import os
import json
from datetime import datetime
from flask import Flask, request, jsonify, render_template
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ==========================
# CONFIG SHEETS
# ==========================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = os.getenv("SHEET_ID", "").strip()   # coloque no Render -> Environment
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Página1").strip()  # nome da aba

def get_client():
    """
    Suporta 2 modos:
    1) SERVICE_ACCOUNT_JSON (conteúdo json inteiro como env)
    2) service_account.json (arquivo no projeto)
    """
    sa_env = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()

    if sa_env:
        info = json.loads(sa_env)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        # arquivo local
        creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)

    return gspread.authorize(creds)

def get_sheet():
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID não configurado nas variáveis de ambiente.")
    client = get_client()
    sh = client.open_by_key(SHEET_ID)
    ws = sh.worksheet(WORKSHEET_NAME)
    return ws

# ==========================
# HELPERS
# ==========================
def parse_data_br(s):
    # aceita "19/02/2026"
    return datetime.strptime(s.strip(), "%d/%m/%Y").date()

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

def pick(row, *keys):
    for k in keys:
        if k in row:
            return row.get(k)
    return None

# ==========================
# ROTAS
# ==========================
@app.get("/")
def home():
    return render_template("index.html")

@app.post("/lancar")
def lancar():
    body = request.get_json(force=True) or {}

    tipo = str(body.get("tipo", "")).strip()
    categoria = str(body.get("categoria", "")).strip()
    descricao = str(body.get("descricao", "")).strip()
    valor = parse_float(body.get("valor"))
    data = str(body.get("data", "")).strip()

    if not tipo or not categoria or not descricao or valor == 0:
        return jsonify({"msg": "Campos obrigatórios: tipo, categoria, descricao, valor"}), 400

    # Se não vier data, usa hoje
    if not data:
        data = datetime.now().strftime("%d/%m/%Y")

    ws = get_sheet()

    # garante cabeçalho
    header = ws.row_values(1)
    if not header or header[:5] != ["Data", "Tipo", "Categoria", "Descrição", "Valor"]:
        ws.update("A1:E1", [["Data", "Tipo", "Categoria", "Descrição", "Valor"]])

    ws.append_row([data, tipo, categoria, descricao, valor])
    return jsonify({"ok": True})

@app.get("/ultimos")
def ultimos():
    ws = get_sheet()
    dados = ws.get_all_records()

    out = []
    # adiciona _row (número real da linha no sheets)
    # get_all_records começa a partir da linha 2
    for idx, r in enumerate(dados, start=2):
        out.append({
            "_row": idx,
            "Data": pick(r, "Data", "data") or "",
            "Tipo": pick(r, "Tipo", "tipo") or "",
            "Categoria": pick(r, "Categoria", "categoria") or "",
            "Descrição": pick(r, "Descrição", "Descricao", "descricao") or "",
            "Valor": pick(r, "Valor", "valor") or 0,
        })

    # retorna todos (o front limita para 10 se quiser)
    return jsonify(out)

@app.get("/resumo")
def resumo():
    """
    /resumo?mes=YYYY-MM  (ex: 2026-02)
    Se não passar mes, usa o mês atual.
    """
    mes = request.args.get("mes")
    hoje = datetime.now().date()
    if not mes:
        mes = hoje.strftime("%Y-%m")
    ano, mm = mes.split("-")
    ano = int(ano)
    mm = int(mm)

    ws = get_sheet()
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
            tipo = str(pick(r, "Tipo", "tipo") or "").strip().lower()
            valor = parse_float(pick(r, "Valor", "valor"))
            categoria = pick(r, "Categoria", "categoria") or ""
            descricao = pick(r, "Descrição", "Descricao", "descricao") or ""

            do_mes.append({
                "data": d.strftime("%d/%m/%Y"),
                "tipo": "Receita" if "rece" in tipo else "Gasto",
                "categoria": str(categoria),
                "descricao": str(descricao),
                "valor": valor,
            })

    entradas = sum(x["valor"] for x in do_mes if x["tipo"] == "Receita")
    saidas   = sum(x["valor"] for x in do_mes if x["tipo"] == "Gasto")
    saldo    = entradas - saidas

    # soma por dia
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

    # ===== PIZZAS POR CATEGORIA =====
    gastos_cat = {}
    receitas_cat = {}
    for x in do_mes:
        cat = x["categoria"] or "Sem categoria"
        if x["tipo"] == "Gasto":
            gastos_cat[cat] = gastos_cat.get(cat, 0.0) + x["valor"]
        else:
            receitas_cat[cat] = receitas_cat.get(cat, 0.0) + x["valor"]

    # ordena desc
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
        "ultimos": do_mes[-10:],
        "qtd": len(do_mes),

        "pizza_gastos_labels": [k for k, v in gastos_sorted],
        "pizza_gastos_values": [v for k, v in gastos_sorted],

        "pizza_receitas_labels": [k for k, v in receitas_sorted],
        "pizza_receitas_values": [v for k, v in receitas_sorted],
    })

@app.patch("/lancamento/<int:row>")
def editar(row):
    body = request.get_json(force=True) or {}

    tipo = str(body.get("tipo", "")).strip()
    categoria = str(body.get("categoria", "")).strip()
    descricao = str(body.get("descricao", "")).strip()
    valor = parse_float(body.get("valor"))
    data = str(body.get("data", "")).strip()

    if not tipo or not categoria or not descricao or not data:
        return jsonify({"msg": "Campos obrigatórios: tipo, categoria, descricao, valor, data"}), 400

    ws = get_sheet()
    # atualiza A..E da linha row
    ws.update(f"A{row}:E{row}", [[data, tipo, categoria, descricao, valor]])
    return jsonify({"ok": True})

@app.delete("/lancamento/<int:row>")
def deletar(row):
    ws = get_sheet()
    ws.delete_rows(row)
    return jsonify({"ok": True})
