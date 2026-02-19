import os
import re
from datetime import datetime, date

import gspread
from flask import Flask, jsonify, render_template, request
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# =========================
# CONFIG
# =========================
SHEET_ID = os.getenv("SHEET_ID", "").strip()
SHEET_TAB = os.getenv("SHEET_TAB", "Lancamentos").strip()

# Render Secret Files geralmente ficam em /etc/secrets/<nome>
CREDS_PATH = os.getenv("GOOGLE_CREDS_PATH", "/etc/secrets/google_creds.json")

# Colunas esperadas (header)
HEADERS = ["Data", "Tipo", "Categoria", "Descrição", "Valor"]


# =========================
# HELPERS
# =========================
def parse_data_br(s: str) -> date:
    """
    Aceita:
      - dd/mm/aaaa
      - aaaa-mm-dd
      - datetime string com esses padrões dentro
    """
    if not s:
        raise ValueError("data vazia")

    s = str(s).strip()

    # dd/mm/aaaa
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    if m:
        dd, mm, yyyy = map(int, m.groups())
        return date(yyyy, mm, dd)

    # aaaa-mm-dd
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        yyyy, mm, dd = map(int, m.groups())
        return date(yyyy, mm, dd)

    # tenta pegar dd/mm/aaaa dentro do texto
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        dd, mm, yyyy = map(int, m.groups())
        return date(yyyy, mm, dd)

    # tenta pegar aaaa-mm-dd dentro do texto
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        yyyy, mm, dd = map(int, m.groups())
        return date(yyyy, mm, dd)

    raise ValueError(f"formato de data inválido: {s}")


def parse_float(v) -> float:
    # aceita 120, "120", "120,50", "1.200,50"
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    v = str(v).strip()
    if not v:
        return 0.0
    v = v.replace(".", "").replace(",", ".")
    try:
        return float(v)
    except Exception:
        return 0.0


def norm_tipo(t: str) -> str:
    t = (t or "").strip().lower()
    if "rece" in t:
        return "Receita"
    return "Gasto"


def pick(row: dict, *keys):
    for k in keys:
        if k in row:
            return row.get(k)
    return None


# =========================
# GOOGLE SHEETS
# =========================
_client = None


def get_sheet():
    global _client

    if not SHEET_ID:
        raise RuntimeError("SHEET_ID não configurado nas Environment Variables.")
    if not os.path.exists(CREDS_PATH):
        raise RuntimeError(
            f"Arquivo de credenciais não encontrado em {CREDS_PATH}. "
            f"Crie o Secret File google_creds.json no Render."
        )

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDS_PATH, scopes=scopes)

    if _client is None:
        _client = gspread.authorize(creds)

    sh = _client.open_by_key(SHEET_ID)
    ws = sh.worksheet(SHEET_TAB)

    # garante cabeçalho na linha 1
    first_row = ws.row_values(1)
    if [c.strip() for c in first_row] != HEADERS:
        if len(first_row) == 0:
            ws.insert_row(HEADERS, 1)
        else:
            # se tem algo diferente, tenta sobrescrever a linha 1
            ws.update("A1:E1", [HEADERS])

    return ws


# =========================
# ROUTES
# =========================
@app.get("/")
def home():
    return render_template("index.html")


@app.post("/lancar")
def lancar():
    data = request.get_json(silent=True) or {}

    tipo = norm_tipo(data.get("tipo"))
    categoria = str(data.get("categoria") or "").strip()
    descricao = str(data.get("descricao") or "").strip()
    valor = parse_float(data.get("valor"))

    data_txt = str(data.get("data") or "").strip()

    if not categoria or not descricao or valor <= 0:
        return jsonify({"ok": False, "msg": "Preencha categoria, descrição e valor (> 0)."}), 400

    # se não veio data, usa hoje
    if not data_txt:
        data_txt = datetime.now().strftime("%d/%m/%Y")

    # valida data
    try:
        d = parse_data_br(data_txt)
        data_txt = d.strftime("%d/%m/%Y")
    except Exception:
        return jsonify({"ok": False, "msg": "Data inválida. Use dd/mm/aaaa."}), 400

    ws = get_sheet()

    # grava no final
    ws.append_row([data_txt, tipo, categoria, descricao, float(valor)], value_input_option="USER_ENTERED")

    return jsonify({"ok": True})


@app.get("/ultimos")
def ultimos():
    ws = get_sheet()
    rows = ws.get_all_records()  # lista de dicts

    # transforma e normaliza
    arr = []
    for r in rows:
        data_txt = pick(r, "Data", "data")
        if not data_txt:
            continue
        try:
            d = parse_data_br(str(data_txt))
        except Exception:
            continue

        tipo = norm_tipo(pick(r, "Tipo", "tipo") or "")
        categoria = str(pick(r, "Categoria", "categoria") or "")
        descricao = str(pick(r, "Descrição", "Descricao", "descricao") or "")
        valor = parse_float(pick(r, "Valor", "valor"))

        arr.append(
            {
                "Data": d.strftime("%d/%m/%Y"),
                "Tipo": tipo,
                "Categoria": categoria,
                "Descrição": descricao,
                "Valor": valor,
                "_ts": d.toordinal(),  # pra ordenar
            }
        )

    # ordena por data
    arr.sort(key=lambda x: x["_ts"])

    # pega últimos 10
    last10 = arr[-10:]
    for x in last10:
        x.pop("_ts", None)

    return jsonify(last10)


@app.get("/resumo")
def resumo():
    """
    /resumo?mes=YYYY-MM (ex: 2026-02)
    Se não passar, usa mês atual.
    Retorna também pizza por categoria (gastos e receitas separados).
    """
    mes = request.args.get("mes")
    hoje = datetime.now().date()

    if not mes:
        mes = hoje.strftime("%Y-%m")

    try:
        ano, mm = mes.split("-")
        ano = int(ano)
        mm = int(mm)
    except Exception:
        return jsonify({"ok": False, "msg": "mes inválido. Use YYYY-MM"}), 400

    ws = get_sheet()
    dados = ws.get_all_records()

    do_mes = []
    for r in dados:
        data_txt = pick(r, "Data", "data")
        if not data_txt:
            continue

        try:
            d = parse_data_br(str(data_txt))
        except Exception:
            continue

        if d.year == ano and d.month == mm:
            tipo = norm_tipo(pick(r, "Tipo", "tipo") or "")
            valor = parse_float(pick(r, "Valor", "valor"))
            categoria = str(pick(r, "Categoria", "categoria") or "").strip() or "Sem categoria"
            descricao = str(pick(r, "Descrição", "Descricao", "descricao") or "")

            do_mes.append(
                {
                    "data": d.strftime("%d/%m/%Y"),
                    "tipo": tipo,  # Receita/Gasto
                    "categoria": categoria,
                    "descricao": descricao,
                    "valor": float(valor),
                    "_d": d,
                }
            )

    entradas = sum(x["valor"] for x in do_mes if x["tipo"] == "Receita")
    saidas = sum(x["valor"] for x in do_mes if x["tipo"] == "Gasto")
    saldo = entradas - saidas

    # ===== Série por dia =====
    por_dia = {}
    for x in do_mes:
        dia = x["_d"].day  # int
        por_dia.setdefault(dia, {"receita": 0.0, "gasto": 0.0})
        if x["tipo"] == "Receita":
            por_dia[dia]["receita"] += x["valor"]
        else:
            por_dia[dia]["gasto"] += x["valor"]

    dias = sorted(por_dia.keys())
    dias_labels = [f"{d:02d}" for d in dias]
    serie_receita = [por_dia[d]["receita"] for d in dias]
    serie_gasto = [por_dia[d]["gasto"] for d in dias]

    # ===== Pizza por categoria (separado) =====
    gastos_cat = {}
    receitas_cat = {}

    for x in do_mes:
        cat = x["categoria"] or "Sem categoria"
        if x["tipo"] == "Gasto":
            gastos_cat[cat] = gastos_cat.get(cat, 0.0) + x["valor"]
        else:
            receitas_cat[cat] = receitas_cat.get(cat, 0.0) + x["valor"]

    # ordena do maior pro menor (fica bonito na pizza)
    gastos_ord = sorted(gastos_cat.items(), key=lambda kv: kv[1], reverse=True)
    receitas_ord = sorted(receitas_cat.items(), key=lambda kv: kv[1], reverse=True)

    pizza_gastos_labels = [k for k, _ in gastos_ord]
    pizza_gastos_values = [v for _, v in gastos_ord]

    pizza_receitas_labels = [k for k, _ in receitas_ord]
    pizza_receitas_values = [v for _, v in receitas_ord]

    # ===== últimos 10 do mês =====
    do_mes.sort(key=lambda x: x["_d"])
    ultimos = do_mes[-10:]
    for x in ultimos:
        x.pop("_d", None)

    return jsonify(
        {
            "mes": mes,
            "entradas": entradas,
            "saidas": saidas,
            "saldo": saldo,
            "dias": dias_labels,
            "serie_receita": serie_receita,
            "serie_gasto": serie_gasto,
            "ultimos": ultimos,
            "qtd": len(do_mes),
            # pizzas
            "pizza_gastos_labels": pizza_gastos_labels,
            "pizza_gastos_values": pizza_gastos_values,
            "pizza_receitas_labels": pizza_receitas_labels,
            "pizza_receitas_values": pizza_receitas_values,
        }
    )


# healthcheck opcional
@app.get("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    # local
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
