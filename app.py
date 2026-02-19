import os
import json
from datetime import datetime, date
from flask import Flask, request, jsonify, render_template

import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__, template_folder="templates")

# ----------------------------
# Google Sheets (Render/ENV)
# ----------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = os.getenv("SHEET_ID", "").strip()  # ID da planilha
SHEET_TAB = os.getenv("SHEET_TAB", "Lancamentos").strip()  # nome da aba (opcional)

def get_sheet():
    """
    Usa a credencial do Service Account vinda do ENV: GOOGLE_CREDS_JSON
    e abre a planilha por SHEET_ID.
    """
    creds_raw = os.getenv("GOOGLE_CREDS_JSON", "").strip()
    if not creds_raw:
        raise RuntimeError("Faltou a variável GOOGLE_CREDS_JSON no Render.")

    if not SHEET_ID:
        raise RuntimeError("Faltou a variável SHEET_ID no Render (ID da planilha).")

    info = json.loads(creds_raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    gc = gspread.authorize(creds)

    sh = gc.open_by_key(SHEET_ID)

    # tenta abrir a aba pelo nome; se não existir, usa a primeira
    try:
        ws = sh.worksheet(SHEET_TAB)
    except Exception:
        ws = sh.sheet1

    # garante cabeçalhos (primeira linha)
    headers = ["Data", "Tipo", "Categoria", "Descrição", "Valor"]
    first_row = ws.row_values(1)
    if [h.strip() for h in first_row] != headers:
        # se a planilha estiver vazia, cria cabeçalho
        if len(first_row) == 0:
            ws.append_row(headers)
        else:
            # se já tem dados mas cabeçalho diferente, não sobrescreve (para não quebrar)
            pass

    return ws

# ----------------------------
# Utilitários
# ----------------------------
def parse_data_br(s: str) -> date:
    # aceita "19/02/2026" e também "2026-02-19"
    s = (s or "").strip()
    if not s:
        raise ValueError("data vazia")
    if "-" in s and len(s) >= 10:
        # yyyy-mm-dd
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    # dd/mm/yyyy
    return datetime.strptime(s, "%d/%m/%Y").date()

def parse_float(v):
    # aceita 120, "120", "120,50"
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

# ----------------------------
# Rotas
# ----------------------------
@app.get("/")
def home():
    # IMPORTANTÍSSIMO: o arquivo tem que estar em /templates/index.html
    return render_template("index.html")

@app.post("/lancar")
def lancar():
    """
    Espera JSON:
    { "tipo": "Gasto"|"Receita", "categoria":"...", "descricao":"...", "valor": 120, "data":"dd/mm/yyyy" (opcional) }
    """
    try:
        body = request.get_json(force=True, silent=True) or {}

        tipo = str(body.get("tipo", "")).strip() or "Gasto"
        categoria = str(body.get("categoria", "")).strip()
        descricao = str(body.get("descricao", "")).strip()
        valor = parse_float(body.get("valor"))

        data_txt = body.get("data")
        if data_txt:
            d = parse_data_br(str(data_txt))
        else:
            d = datetime.now().date()

        if not categoria or not descricao or valor <= 0:
            return jsonify({"ok": False, "msg": "Preencha categoria, descrição e valor (>0)."}), 400

        ws = get_sheet()
        ws.append_row([
            d.strftime("%d/%m/%Y"),
            "Receita" if "rece" in tipo.lower() else "Gasto",
            categoria,
            descricao,
            valor
        ])

        return jsonify({"ok": True, "msg": "Lançamento salvo!"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

@app.get("/ultimos")
def ultimos():
    """
    Retorna até 10 últimos lançamentos (JSON list).
    """
    try:
        ws = get_sheet()
        dados = ws.get_all_records()  # lista de dicts

        # pega os 10 últimos (mantendo ordem original e devolvendo list simples)
        ult = dados[-10:] if len(dados) > 10 else dados

        return jsonify(ult)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

@app.get("/resumo")
def resumo():
    """
    /resumo?mes=YYYY-MM  (ex: 2026-02)
    Se não passar mes, usa o mês atual.
    """
    try:
        mes = request.args.get("mes")
        hoje = datetime.now().date()
        if not mes:
            mes = hoje.strftime("%Y-%m")

        ano_s, mm_s = mes.split("-")
        ano = int(ano_s)
        mm = int(mm_s)

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

        por_dia = {}
        for x in do_mes:
            dia = x["data"][:2]  # "dd"
            por_dia.setdefault(dia, {"receita": 0.0, "gasto": 0.0})
            if x["tipo"] == "Receita":
                por_dia[dia]["receita"] += x["valor"]
            else:
                por_dia[dia]["gasto"] += x["valor"]

        dias = sorted(por_dia.keys(), key=lambda z: int(z))
        serie_receita = [por_dia[d]["receita"] for d in dias]
        serie_gasto   = [por_dia[d]["gasto"] for d in dias]

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
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

# Render/Gunicorn usa "app:app", então não precisa de app.run()
