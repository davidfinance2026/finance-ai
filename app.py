import os
import json
from datetime import datetime, date
from functools import lru_cache

from flask import Flask, request, jsonify, render_template
import gspread
from google.oauth2.service_account import Credentials


app = Flask(__name__)

# =========================
# CONFIG (Render / Local)
# =========================
# No Render, crie env vars:
# - SHEET_ID  (ID da planilha)
# - SHEET_TAB (opcional, nome da aba; default "Lançamentos")
# - GOOGLE_CREDS_JSON (JSON da Service Account, inteiro, como string)
#
# Alternativa local: colocar um arquivo service_account.json na raiz
# e NÃO usar GOOGLE_CREDS_JSON.

SHEET_ID = os.environ.get("SHEET_ID", "").strip()
SHEET_TAB = os.environ.get("SHEET_TAB", "Lançamentos").strip()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# =========================
# HELPERS
# =========================
def parse_data_br(s: str) -> date:
    # aceita "dd/mm/aaaa" e também "aaaa-mm-dd"
    s = (s or "").strip()
    if not s:
        raise ValueError("data vazia")

    if "/" in s:
        d, m, y = s.split("/")
        return date(int(y), int(m), int(d))
    if "-" in s:
        y, m, d = s.split("-")
        return date(int(y), int(m), int(d))

    raise ValueError("formato de data inválido")


def parse_float(v):
    # aceita 120, "120", "120,50", "1.234,56"
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    v = str(v).strip().replace(".", "").replace(",", ".")
    try:
        return float(v)
    except:
        return 0.0


def normalize_tipo(t: str) -> str:
    t = (t or "").strip().lower()
    if "rece" in t:
        return "Receita"
    return "Gasto"


def safe_str(x):
    return "" if x is None else str(x).strip()


@lru_cache(maxsize=1)
def get_client():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        return gspread.authorize(creds)

    # fallback local
    if os.path.exists("service_account.json"):
        creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
        return gspread.authorize(creds)

    raise RuntimeError("Credenciais não encontradas. Use GOOGLE_CREDS_JSON ou service_account.json")


def get_sheet():
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID não definido nas variáveis de ambiente.")
    gc = get_client()
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(SHEET_TAB)
    return ws


def ensure_header(ws):
    # garante o cabeçalho padrão na 1ª linha
    header = ws.row_values(1)
    expected = ["Data", "Tipo", "Categoria", "Descrição", "Valor"]
    if [h.strip() for h in header] != expected:
        # se planilha vazia ou diferente, cria/ajusta cabeçalho
        ws.update("A1:E1", [expected])


def pick(row, *keys):
    for k in keys:
        if k in row:
            return row.get(k)
    return None


# =========================
# ROUTES
# =========================
@app.get("/")
def home():
    # precisa existir /templates/index.html
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/lancar")
def lancar():
    try:
        payload = request.get_json(force=True, silent=True) or {}

        tipo = normalize_tipo(payload.get("tipo"))
        categoria = safe_str(payload.get("categoria"))
        descricao = safe_str(payload.get("descricao"))
        valor = parse_float(payload.get("valor"))
        data_txt = safe_str(payload.get("data"))

        if not categoria or not descricao or valor <= 0:
            return jsonify({"ok": False, "msg": "Preencha categoria, descrição e valor (> 0)."}), 400

        # data opcional: se não vier, usa hoje
        if data_txt:
            d = parse_data_br(data_txt)
        else:
            d = datetime.now().date()

        data_br = d.strftime("%d/%m/%Y")

        ws = get_sheet()
        ensure_header(ws)
        ws.append_row([data_br, tipo, categoria, descricao, valor], value_input_option="USER_ENTERED")

        return jsonify({"ok": True, "msg": "Lançamento salvo!"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Erro ao salvar: {e}"}), 500


@app.get("/ultimos")
def ultimos():
    try:
        ws = get_sheet()
        ensure_header(ws)

        # pega tudo e devolve os últimos 10
        records = ws.get_all_records()  # lista de dicts
        # normaliza para ter sempre as mesmas chaves
        out = []
        for r in records[-10:]:
            out.append({
                "Data": safe_str(pick(r, "Data", "data")),
                "Tipo": normalize_tipo(pick(r, "Tipo", "tipo")),
                "Categoria": safe_str(pick(r, "Categoria", "categoria")),
                "Descrição": safe_str(pick(r, "Descrição", "Descricao", "descricao")),
                "Valor": parse_float(pick(r, "Valor", "valor")),
            })

        return jsonify(out)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Erro em /ultimos: {e}"}), 500


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

        ano, mm = mes.split("-")
        ano = int(ano)
        mm = int(mm)

        ws = get_sheet()
        ensure_header(ws)
        dados = ws.get_all_records()  # lista de dicts

        # filtra apenas lançamentos do mês
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
                tipo = normalize_tipo(pick(r, "Tipo", "tipo"))
                valor = parse_float(pick(r, "Valor", "valor"))
                categoria = safe_str(pick(r, "Categoria", "categoria"))
                descricao = safe_str(pick(r, "Descrição", "Descricao", "descricao"))
                do_mes.append({
                    "data": d.strftime("%d/%m/%Y"),
                    "tipo": tipo,
                    "categoria": categoria,
                    "descricao": descricao,
                    "valor": valor,
                })

        entradas = sum(x["valor"] for x in do_mes if x["tipo"] == "Receita")
        saidas   = sum(x["valor"] for x in do_mes if x["tipo"] == "Gasto")
        saldo    = entradas - saidas

        # gráfico (barras/linha): soma por dia do mês
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

        # =========================
        # PIZZA: Top 6 + Outros
        # =========================
        gastos_por_categoria = {}
        for x in do_mes:
            if x["tipo"] != "Gasto":
                continue
            cat = (x["categoria"] or "Sem categoria").strip() or "Sem categoria"
            gastos_por_categoria[cat] = gastos_por_categoria.get(cat, 0.0) + float(x["valor"] or 0.0)

        itens = sorted(gastos_por_categoria.items(), key=lambda kv: kv[1], reverse=True)

        top_n = 6
        top = itens[:top_n]
        resto = itens[top_n:]

        cats_gasto = [k for k, _ in top]
        vals_gasto = [v for _, v in top]

        if resto:
            outros_total = sum(v for _, v in resto)
            cats_gasto.append("Outros")
            vals_gasto.append(outros_total)

        return jsonify({
            "mes": mes,
            "entradas": entradas,
            "saidas": saidas,
            "saldo": saldo,
            "dias": dias,
            "serie_receita": serie_receita,
            "serie_gasto": serie_gasto,
            "cats_gasto": cats_gasto,
            "vals_gasto": vals_gasto,
            "ultimos": do_mes[-10:],
            "qtd": len(do_mes),
        })

    except Exception as e:
        return jsonify({"ok": False, "msg": f"Erro em /resumo: {e}"}), 500


if __name__ == "__main__":
    app.run(debug=True)
