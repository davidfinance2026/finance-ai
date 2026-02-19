import os
import json
from datetime import datetime
from functools import lru_cache

import gspread
from flask import Flask, jsonify, render_template, request
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_TAB = "Lancamentos"  # ou "Lançamentos" se sua aba tiver acento

app = Flask(__name__, template_folder="templates")


@lru_cache(maxsize=1)
def get_client():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        return gspread.authorize(creds)

    secret_path = "/etc/secrets/google_creds.json"
    if os.path.exists(secret_path):
        creds = Credentials.from_service_account_file(secret_path, scopes=SCOPES)
        return gspread.authorize(creds)

    if os.path.exists("service_account.json"):
        creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
        return gspread.authorize(creds)

    raise RuntimeError("Credenciais não encontradas. Configure GOOGLE_CREDS_JSON ou /etc/secrets/google_creds.json")


def get_sheet():
    sheet_id = os.environ.get("SHEET_ID", "").strip()
    tab_name = os.environ.get("SHEET_TAB", DEFAULT_TAB).strip()

    if not sheet_id:
        raise RuntimeError("SHEET_ID não definido nas Environment Variables do Render.")

    gc = get_client()
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(tab_name)
    return ws


def parse_data_br(s: str):
    s = (s or "").strip()
    if not s:
        raise ValueError("data vazia")
    if "-" in s and len(s) >= 10:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    return datetime.strptime(s, "%d/%m/%Y").date()


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


def normalize_cat(cat: str):
    return (cat or "").strip().title()


@app.get("/")
def home():
    return render_template("index.html")


@app.post("/lancar")
def lancar():
    try:
        body = request.get_json(force=True, silent=True) or {}

        tipo = str(body.get("tipo", "")).strip() or "Gasto"
        categoria = str(body.get("categoria", "")).strip()
        descricao = str(body.get("descricao", "")).strip()
        valor = parse_float(body.get("valor"))
        data_txt = body.get("data")

        if not categoria or not descricao or valor == 0:
            return jsonify({"ok": False, "msg": "Informe categoria, descrição e valor."}), 400

        if data_txt:
            d = parse_data_br(str(data_txt))
        else:
            d = datetime.now().date()

        data_br = d.strftime("%d/%m/%Y")

        ws = get_sheet()
        ws.append_row([data_br, tipo, categoria, descricao, valor], value_input_option="USER_ENTERED")

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.get("/ultimos")
def ultimos():
    try:
        ws = get_sheet()
        dados = ws.get_all_records()
        ult = dados[-10:] if len(dados) > 10 else dados
        return jsonify(ult)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.get("/resumo")
def resumo():
    try:
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
                tipo_raw = str(pick(r, "Tipo", "tipo") or "").strip().lower()
                valor = parse_float(pick(r, "Valor", "valor"))
                categoria = pick(r, "Categoria", "categoria") or ""
                descricao = pick(r, "Descrição", "Descricao", "descricao") or ""

                tipo_norm = "Receita" if "rece" in tipo_raw else "Gasto"

                do_mes.append({
                    "data": d.strftime("%d/%m/%Y"),
                    "tipo": tipo_norm,
                    "categoria": str(categoria),
                    "descricao": str(descricao),
                    "valor": valor,
                })

        entradas = sum(x["valor"] for x in do_mes if x["tipo"] == "Receita")
        saidas = sum(x["valor"] for x in do_mes if x["tipo"] == "Gasto")
        saldo = entradas - saidas

        # séries por dia
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

        # pizzas separadas
        por_cat_gastos = {}
        por_cat_receitas = {}

        for x in do_mes:
            cat = normalize_cat(x.get("categoria"))
            val = float(x.get("valor") or 0.0)

            if x["tipo"] == "Gasto":
                por_cat_gastos[cat] = por_cat_gastos.get(cat, 0.0) + val
            else:
                por_cat_receitas[cat] = por_cat_receitas.get(cat, 0.0) + val

        gastos_sorted = sorted(por_cat_gastos.items(), key=lambda kv: kv[1], reverse=True)
        receitas_sorted = sorted(por_cat_receitas.items(), key=lambda kv: kv[1], reverse=True)

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

            "pizza_gastos_labels": [k for k, _ in gastos_sorted],
            "pizza_gastos_values": [v for _, v in gastos_sorted],

            "pizza_receitas_labels": [k for k, _ in receitas_sorted],
            "pizza_receitas_values": [v for _, v in receitas_sorted],
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
