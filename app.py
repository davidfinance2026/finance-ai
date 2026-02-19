import os
from datetime import datetime
from collections import defaultdict

from flask import Flask, jsonify, request, render_template
import gspread
from google.oauth2.service_account import Credentials


app = Flask(__name__)

# ====== CONFIG ======
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Render Secret File: /etc/secrets/google_creds.json
CREDS_PATH = os.getenv("GOOGLE_CREDS_PATH", "/etc/secrets/google_creds.json")

SHEET_ID = os.getenv("SHEET_ID", "").strip()
SHEET_TAB = os.getenv("SHEET_TAB", "Lançamentos").strip()

# Cabeçalhos esperados na planilha
HEADERS = ["Data", "Tipo", "Categoria", "Descrição", "Valor"]


# ====== GOOGLE SHEETS HELPERS ======
_client = None

def get_client():
    global _client
    if _client is not None:
        return _client

    if not os.path.exists(CREDS_PATH):
        raise FileNotFoundError(
            f"Arquivo de credenciais não encontrado: {CREDS_PATH}. "
            f"No Render, crie Secret File 'google_creds.json' e ele ficará em /etc/secrets/google_creds.json"
        )

    creds = Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)
    _client = gspread.authorize(creds)
    return _client


def get_ws():
    if not SHEET_ID:
        raise ValueError("SHEET_ID não definido nas Environment Variables do Render.")

    sh = get_client().open_by_key(SHEET_ID)

    try:
        ws = sh.worksheet(SHEET_TAB)
    except Exception:
        # tenta achar por nome parecido
        tabs = [w.title for w in sh.worksheets()]
        raise ValueError(
            f"Aba '{SHEET_TAB}' não encontrada. Abas disponíveis: {tabs}. "
            f"Ajuste SHEET_TAB no Render exatamente igual ao nome da aba."
        )

    # Garante cabeçalho
    header_row = ws.row_values(1)
    if [h.strip() for h in header_row[:len(HEADERS)]] != HEADERS:
        # se a planilha estiver vazia ou errada, corrige linha 1
        ws.update("A1:E1", [HEADERS])

    return ws


def parse_ddmmyyyy(s: str):
    # aceita dd/mm/aaaa
    return datetime.strptime(s, "%d/%m/%Y").date()


def normalize_tipo(t: str):
    t = (t or "").strip().lower()
    if "rece" in t:
        return "Receita"
    return "Gasto"


def safe_float(x):
    try:
        return float(str(x).replace(",", "."))
    except Exception:
        return 0.0


def row_to_dict(values, row_number):
    # values = [Data, Tipo, Categoria, Descrição, Valor]
    out = {}
    for i, h in enumerate(HEADERS):
        out[h] = values[i] if i < len(values) else ""
    out["_row"] = row_number
    return out


def read_all_rows():
    ws = get_ws()
    all_vals = ws.get_all_values()  # inclui cabeçalho
    if len(all_vals) <= 1:
        return []
    data_rows = all_vals[1:]  # sem header
    # row number real na planilha começa em 2
    result = []
    for idx, row in enumerate(data_rows, start=2):
        if not any(cell.strip() for cell in row):
            continue
        result.append(row_to_dict(row, idx))
    return result


# ====== ROUTES ======
@app.get("/")
def home():
    return render_template("index.html")


@app.post("/lancar")
def lancar():
    try:
        body = request.get_json(force=True) or {}

        tipo = normalize_tipo(body.get("tipo"))
        categoria = (body.get("categoria") or "").strip()
        descricao = (body.get("descricao") or "").strip()
        valor = safe_float(body.get("valor"))
        data = (body.get("data") or "").strip()  # dd/mm/aaaa

        if not categoria or not descricao or not data or valor <= 0:
            return jsonify({"msg": "Preencha: categoria, descrição, data (dd/mm/aaaa) e valor > 0."}), 400

        # valida data
        _ = parse_ddmmyyyy(data)

        ws = get_ws()
        ws.append_row([data, tipo, categoria, descricao, valor], value_input_option="USER_ENTERED")

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"msg": str(e)}), 500


@app.get("/ultimos")
def ultimos():
    try:
        rows = read_all_rows()

        # ordena por data e por row (mais novo no final)
        def key_fn(r):
            try:
                d = parse_ddmmyyyy(r["Data"])
            except Exception:
                d = datetime(1970, 1, 1).date()
            return (d, r["_row"])

        rows.sort(key=key_fn)
        # últimos 10
        return jsonify(rows[-10:])
    except Exception as e:
        return jsonify({"msg": str(e)}), 500


@app.get("/resumo")
def resumo():
    try:
        rows = read_all_rows()
        hoje = datetime.now().date()
        mes = hoje.month
        ano = hoje.year

        entradas = 0.0
        saidas = 0.0

        # séries por dia do mês
        dias_no_mes = 31  # simplificado, depois recorta pelo máximo encontrado
        receita_por_dia = defaultdict(float)
        gasto_por_dia = defaultdict(float)

        # pizzas por categoria
        pizza_gastos = defaultdict(float)
        pizza_receitas = defaultdict(float)

        for r in rows:
            try:
                d = parse_ddmmyyyy(r["Data"])
            except Exception:
                continue

            if d.month != mes or d.year != ano:
                continue

            tipo = normalize_tipo(r.get("Tipo"))
            cat = (r.get("Categoria") or "").strip() or "Sem categoria"
            val = safe_float(r.get("Valor"))

            if tipo == "Receita":
                entradas += val
                receita_por_dia[d.day] += val
                pizza_receitas[cat] += val
            else:
                saidas += val
                gasto_por_dia[d.day] += val
                pizza_gastos[cat] += val

        saldo = entradas - saidas

        # define dias (1..max dia visto) para o gráfico ficar enxuto
        max_day = 1
        for k in list(receita_por_dia.keys()) + list(gasto_por_dia.keys()):
            max_day = max(max_day, int(k))

        dias = list(range(1, max_day + 1))
        serie_receita = [round(receita_por_dia[d], 2) for d in dias]
        serie_gasto = [round(gasto_por_dia[d], 2) for d in dias]

        # pizzas (labels e values)
        def dict_to_pie(dct):
            items = sorted(dct.items(), key=lambda x: x[1], reverse=True)
            labels = [k for k, _ in items]
            values = [round(v, 2) for _, v in items]
            return labels, values

        pg_labels, pg_values = dict_to_pie(pizza_gastos)
        pr_labels, pr_values = dict_to_pie(pizza_receitas)

        return jsonify({
            "entradas": round(entradas, 2),
            "saidas": round(saidas, 2),
            "saldo": round(saldo, 2),
            "dias": [str(d) for d in dias],
            "serie_receita": serie_receita,
            "serie_gasto": serie_gasto,

            "pizza_gastos_labels": pg_labels,
            "pizza_gastos_values": pg_values,
            "pizza_receitas_labels": pr_labels,
            "pizza_receitas_values": pr_values,
        })
    except Exception as e:
        return jsonify({"msg": str(e)}), 500


# ====== EDITAR / EXCLUIR ======
@app.patch("/lancamento/<int:row>")
def editar(row: int):
    try:
        body = request.get_json(force=True) or {}

        tipo = normalize_tipo(body.get("tipo"))
        categoria = (body.get("categoria") or "").strip()
        descricao = (body.get("descricao") or "").strip()
        valor = safe_float(body.get("valor"))
        data = (body.get("data") or "").strip()

        if not categoria or not descricao or not data or valor <= 0:
            return jsonify({"msg": "Preencha: categoria, descrição, data (dd/mm/aaaa) e valor > 0."}), 400

        _ = parse_ddmmyyyy(data)

        ws = get_ws()
        # Atualiza A..E da linha informada
        ws.update(f"A{row}:E{row}", [[data, tipo, categoria, descricao, valor]])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"msg": str(e)}), 500


@app.delete("/lancamento/<int:row>")
def excluir(row: int):
    try:
        ws = get_ws()
        ws.delete_rows(row)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"msg": str(e)}), 500


# ====== LOCAL DEV ======
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=True)
