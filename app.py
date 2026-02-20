import os
from datetime import datetime
from collections import defaultdict

from flask import Flask, jsonify, render_template, request
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# =========================
# CONFIG
# =========================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = os.getenv("SHEET_ID", "").strip()
SHEET_TAB = os.getenv("SHEET_TAB", "Lançamentos").strip()

# Render Secret Files ficam em /etc/secrets/<filename>
CREDS_PATH = os.getenv("GOOGLE_CREDS_PATH", "/etc/secrets/google_creds.json")


# =========================
# GOOGLE SHEETS HELPERS
# =========================
def _get_creds_file():
    # prioridade: /etc/secrets/google_creds.json
    if CREDS_PATH and os.path.exists(CREDS_PATH):
        return CREDS_PATH
    # fallback: arquivo no repo
    if os.path.exists("google_creds.json"):
        return "google_creds.json"
    if os.path.exists("service_account.json"):
        return "service_account.json"
    raise FileNotFoundError(
        "Arquivo de credenciais não encontrado. "
        "No Render, crie um Secret File chamado google_creds.json "
        "e/ou defina GOOGLE_CREDS_PATH."
    )


def get_client():
    creds_file = _get_creds_file()
    creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    return gspread.authorize(creds)


def get_sheet():
    if not SHEET_ID:
        raise ValueError("SHEET_ID não configurado no Render.")
    client = get_client()
    sh = client.open_by_key(SHEET_ID)
    return sh.worksheet(SHEET_TAB)


# =========================
# DATA / NORMALIZAÇÃO
# =========================
def parse_date_br(s: str) -> datetime.date:
    # aceita dd/mm/aaaa
    return datetime.strptime(s.strip(), "%d/%m/%Y").date()


def normalize_tipo(s: str) -> str:
    s = (s or "").strip().lower()
    return "Receita" if "rece" in s else "Gasto"


def normalize_categoria(s: str) -> str:
    # mantém acentos, mas normaliza espaços/case
    return " ".join((s or "").strip().split()).title()


def to_float(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return 0.0
    # aceita "120,50" e "120.50"
    s = s.replace(".", "").replace(",", ".") if s.count(",") == 1 and s.count(".") >= 1 else s.replace(",", ".")
    try:
        return float(s)
    except:
        return 0.0


def month_key(d):
    return (d.year, d.month)


def today_month_key():
    h = datetime.now().date()
    return (h.year, h.month)


def build_rows(ws):
    """
    Lê planilha e devolve lista de dicts:
    {Data, Tipo, Categoria, Descrição, Valor, _row}
    _row é a linha real na planilha (começa em 2).
    """
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return []

    header = values[0]
    # Esperado: Data, Tipo, Categoria, Descrição, Valor
    # Mas vamos ser tolerantes pelo nome.
    def col_index(name):
        for i, h in enumerate(header):
            if h.strip().lower() == name.strip().lower():
                return i
        return None

    idx_data = col_index("Data")
    idx_tipo = col_index("Tipo")
    idx_cat = col_index("Categoria")
    idx_desc = col_index("Descrição") if col_index("Descrição") is not None else col_index("Descricao")
    idx_val = col_index("Valor")

    out = []
    for i, row in enumerate(values[1:], start=2):  # start=2 pois linha 1 é header
        if not any(cell.strip() for cell in row):
            continue

        data = row[idx_data] if idx_data is not None and idx_data < len(row) else ""
        tipo = row[idx_tipo] if idx_tipo is not None and idx_tipo < len(row) else ""
        cat = row[idx_cat] if idx_cat is not None and idx_cat < len(row) else ""
        desc = row[idx_desc] if idx_desc is not None and idx_desc < len(row) else ""
        val = row[idx_val] if idx_val is not None and idx_val < len(row) else ""

        out.append({
            "Data": data,
            "Tipo": tipo,
            "Categoria": cat,
            "Descrição": desc,
            "Valor": val,
            "_row": i
        })
    return out


# =========================
# ROUTES
# =========================
@app.get("/")
def home():
    return render_template("index.html")


@app.post("/lancar")
def lancar():
    try:
        payload = request.get_json(force=True) or {}
        tipo = normalize_tipo(payload.get("tipo"))
        categoria = normalize_categoria(payload.get("categoria"))
        descricao = (payload.get("descricao") or "").strip()
        valor = to_float(payload.get("valor"))
        data = (payload.get("data") or "").strip()  # dd/mm/aaaa

        if not categoria or not descricao or not data or valor <= 0:
            return jsonify({"msg": "Preencha categoria, descrição, data (dd/mm/aaaa) e valor > 0."}), 400

        # valida data
        _ = parse_date_br(data)

        ws = get_sheet()
        ws.append_row([data, tipo, categoria, descricao, str(valor)], value_input_option="USER_ENTERED")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"msg": str(e)}), 500


@app.get("/ultimos")
def ultimos():
    try:
        ws = get_sheet()
        rows = build_rows(ws)

        # ordena por data (desc)
        def key_fn(item):
            try:
                d = parse_date_br(item.get("Data", "01/01/1970"))
            except:
                d = datetime(1970, 1, 1).date()
            return d

        rows.sort(key=key_fn)
        # pega os últimos 10
        last = rows[-10:] if len(rows) > 10 else rows

        return jsonify(last)
    except Exception as e:
        return jsonify({"msg": str(e)}), 500


@app.get("/resumo")
def resumo():
    try:
        ws = get_sheet()
        rows = build_rows(ws)

        mk = today_month_key()

        entradas = 0.0
        saidas = 0.0

        # séries do mês por dia
        receita_por_dia = defaultdict(float)
        gasto_por_dia = defaultdict(float)

        # pizzas por categoria do mês
        gasto_por_cat = defaultdict(float)
        receita_por_cat = defaultdict(float)

        for r in rows:
            data_s = (r.get("Data") or "").strip()
            tipo = normalize_tipo(r.get("Tipo"))
            cat = normalize_categoria(r.get("Categoria"))
            val = to_float(r.get("Valor"))

            try:
                d = parse_date_br(data_s)
            except:
                continue

            if month_key(d) != mk:
                continue

            dia = d.day

            if tipo == "Receita":
                entradas += val
                receita_por_dia[dia] += val
                receita_por_cat[cat] += val
            else:
                saidas += val
                gasto_por_dia[dia] += val
                gasto_por_cat[cat] += val

        saldo = entradas - saidas

        # construir lista de dias do mês (1..hoje)
        hoje = datetime.now().date()
        dias = list(range(1, hoje.day + 1))

        serie_receita = [round(receita_por_dia[d], 2) for d in dias]
        serie_gasto = [round(gasto_por_dia[d], 2) for d in dias]

        # pizzas - ordena por valor desc e limita para não ficar enorme
        def top_n_dict(dct, n=12):
            items = sorted(dct.items(), key=lambda x: x[1], reverse=True)
            return items[:n]

        top_g = top_n_dict(gasto_por_cat, 12)
        top_r = top_n_dict(receita_por_cat, 12)

        resp = {
            "entradas": round(entradas, 2),
            "saidas": round(saidas, 2),
            "saldo": round(saldo, 2),

            "dias": [str(d) for d in dias],
            "serie_receita": serie_receita,
            "serie_gasto": serie_gasto,

            # pizzas
            "pizza_gastos_labels": [k for k, _ in top_g],
            "pizza_gastos_values": [round(v, 2) for _, v in top_g],
            "pizza_receitas_labels": [k for k, _ in top_r],
            "pizza_receitas_values": [round(v, 2) for _, v in top_r],
        }

        return jsonify(resp)
    except Exception as e:
        return jsonify({"msg": str(e)}), 500


@app.patch("/lancamento/<int:row>")
def editar_lancamento(row: int):
    """
    row = linha real na planilha (>=2)
    Atualiza as colunas A..E
    """
    try:
        payload = request.get_json(force=True) or {}
        tipo = normalize_tipo(payload.get("tipo"))
        categoria = normalize_categoria(payload.get("categoria"))
        descricao = (payload.get("descricao") or "").strip()
        valor = to_float(payload.get("valor"))
        data = (payload.get("data") or "").strip()

        if row < 2:
            return jsonify({"msg": "Linha inválida."}), 400

        if not categoria or not descricao or not data or valor <= 0:
            return jsonify({"msg": "Preencha categoria, descrição, data (dd/mm/aaaa) e valor > 0."}), 400

        _ = parse_date_br(data)  # valida

        ws = get_sheet()
        # Atualiza A..E na linha
        ws.update(f"A{row}:E{row}", [[data, tipo, categoria, descricao, str(valor)]], value_input_option="USER_ENTERED")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"msg": str(e)}), 500


@app.delete("/lancamento/<int:row>")
def excluir_lancamento(row: int):
    try:
        if row < 2:
            return jsonify({"msg": "Linha inválida."}), 400

        ws = get_sheet()
        ws.delete_rows(row)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"msg": str(e)}), 500


if __name__ == "__main__":
    # local
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
