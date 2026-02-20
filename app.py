import os
import json
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_client_cached = None

def get_client() -> gspread.Client:
    """
    Prioridade:
    1) SERVICE_ACCOUNT_JSON (env com JSON inteiro)
    2) Secret File do Render em /etc/secrets/google_creds.json
    3) arquivo local google_creds.json (caso rode local)
    """
    global _client_cached
    if _client_cached is not None:
        return _client_cached

    # 1) JSON via variável de ambiente
    raw = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        info = json.loads(raw)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        _client_cached = gspread.authorize(creds)
        return _client_cached

    # 2) Secret file no Render
    secret_path = "/etc/secrets/google_creds.json"
    if os.path.exists(secret_path):
        creds = Credentials.from_service_account_file(secret_path, scopes=SCOPES)
        _client_cached = gspread.authorize(creds)
        return _client_cached

    # 3) fallback local
    local_path = "google_creds.json"
    if os.path.exists(local_path):
        creds = Credentials.from_service_account_file(local_path, scopes=SCOPES)
        _client_cached = gspread.authorize(creds)
        return _client_cached

    raise RuntimeError(
        "Credenciais não encontradas. "
        "Crie SERVICE_ACCOUNT_JSON OU envie Secret File 'google_creds.json' no Render."
    )


def get_sheet() -> gspread.Worksheet:
    """
    Usa:
    - SHEET_ID (obrigatório)
    - SHEET_TAB (seu env atual) OU WORKSHEET_NAME (alternativo)
    """
    sheet_id = os.getenv("SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("Missing env var SHEET_ID")

    client = get_client()
    sh = client.open_by_key(sheet_id)

    # seu env atual:
    ws_name = os.getenv("SHEET_TAB", "").strip()
    # compatível com outras versões:
    if not ws_name:
        ws_name = os.getenv("WORKSHEET_NAME", "").strip()

    ws = sh.worksheet(ws_name) if ws_name else sh.get_worksheet(0)

    # garante cabeçalho
    HEADERS = ["ID", "Data", "Tipo", "Categoria", "Descrição", "Valor", "CreatedAt"]
    values = ws.get_all_values()
    if not values or not values[0]:
        ws.append_row(HEADERS)
    else:
        first = [c.strip() for c in values[0]]
        if first != HEADERS:
            ws.update("A1", [HEADERS])

    return wsimport os
import json
import uuid
import csv
import io
from datetime import datetime, date
from calendar import monthrange
from typing import Any, Dict, List, Tuple

from flask import Flask, jsonify, request, render_template, Response, abort
import gspread
from google.oauth2.service_account import Credentials

# PDF (reportlab)
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as pdf_canvas


app = Flask(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = ["ID", "Data", "Tipo", "Categoria", "Descrição", "Valor", "CreatedAt"]


# =========================
# AUTH (senha simples)
# =========================
def require_auth():
    pwd = os.getenv("APP_PASSWORD", "").strip()
    if not pwd:
        return  # sem senha -> sem auth

    token = request.headers.get("X-APP-TOKEN", "").strip()
    if token != pwd:
        abort(401)


@app.before_request
def _auth_middleware():
    # deixa a home carregar sem auth? -> NÃO (mais seguro)
    # se quiser liberar home, comenta o bloco abaixo e deixa só nas rotas de API
    require_auth()


# =========================
# GOOGLE SHEETS
# =========================
_client_cached = None

def get_client() -> gspread.Client:
    global _client_cached
    if _client_cached is not None:
        return _client_cached

    raw = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("Missing env var SERVICE_ACCOUNT_JSON")

    try:
        info = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"SERVICE_ACCOUNT_JSON is not valid JSON: {e}")

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    _client_cached = gspread.authorize(creds)
    return _client_cached


def get_sheet() -> gspread.Worksheet:
    sheet_id = os.getenv("SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("Missing env var SHEET_ID")

    client = get_client()
    sh = client.open_by_key(sheet_id)

    ws_name = os.getenv("WORKSHEET_NAME", "").strip()
    ws = sh.worksheet(ws_name) if ws_name else sh.get_worksheet(0)

    # garante cabeçalho
    values = ws.get_all_values()
    if not values or not values[0]:
        ws.append_row(HEADERS)
    else:
        first = values[0]
        if [c.strip() for c in first] != HEADERS:
            # se já tem algo, não destrói — apenas garante que exista header consistente
            # mas aqui a gente força o header se estiver vazio ou diferente
            ws.update("A1", [HEADERS])

    return ws


def parse_br_date(s: str) -> date:
    # "dd/mm/aaaa"
    return datetime.strptime(s.strip(), "%d/%m/%Y").date()


def safe_float(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    txt = str(v).strip().replace("R$", "").replace(".", "").replace(",", ".")
    try:
        return float(txt)
    except:
        return 0.0


def get_rows_with_rownum(ws: gspread.Worksheet) -> Tuple[List[str], List[Dict[str, Any]]]:
    """
    Retorna headers e lista de dicts com _row (número da linha real no Sheets).
    """
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return HEADERS, []

    headers = values[0]
    data_rows = values[1:]

    out = []
    for idx, row in enumerate(data_rows, start=2):  # linha 2 = primeiro lançamento
        obj = {}
        for h_i, h in enumerate(headers):
            obj[h] = row[h_i] if h_i < len(row) else ""
        obj["_row"] = idx
        out.append(obj)

    return headers, out


def filter_rows(rows: List[Dict[str, Any]], month: int, year: int, q: str) -> List[Dict[str, Any]]:
    q = (q or "").strip().lower()

    filtered = []
    for r in rows:
        d_str = (r.get("Data") or "").strip()
        try:
            d = parse_br_date(d_str)
        except:
            continue

        if d.month != month or d.year != year:
            continue

        if q:
            hay = " ".join([
                str(r.get("Tipo", "")),
                str(r.get("Categoria", "")),
                str(r.get("Descrição", "")),
                str(r.get("Valor", "")),
                str(r.get("Data", "")),
            ]).lower()
            if q not in hay:
                continue

        filtered.append(r)

    return filtered


def get_month_year_from_request() -> Tuple[int, int]:
    today = datetime.now().date()
    month = request.args.get("month", default=today.month, type=int)
    year = request.args.get("year", default=today.year, type=int)
    if month < 1 or month > 12:
        month = today.month
    if year < 1900:
        year = today.year
    return month, year


# =========================
# ROUTES
# =========================
@app.get("/")
def home():
    return render_template("index.html")


@app.post("/login")
def login():
    # login simples: manda password e valida com APP_PASSWORD
    pwd = os.getenv("APP_PASSWORD", "").strip()
    if not pwd:
        return jsonify({"ok": True, "token": ""})

    body = request.get_json(force=True, silent=True) or {}
    password = str(body.get("password", "")).strip()
    if password != pwd:
        return jsonify({"ok": False, "msg": "Senha inválida"}), 401
    return jsonify({"ok": True, "token": password})


@app.post("/lancar")
def lancar():
    ws = get_sheet()
    body = request.get_json(force=True, silent=True) or {}

    tipo = str(body.get("tipo", "")).strip()
    categoria = str(body.get("categoria", "")).strip()
    descricao = str(body.get("descricao", "")).strip()
    valor = body.get("valor", None)
    data_str = str(body.get("data", "")).strip()  # dd/mm/aaaa

    if tipo not in ("Gasto", "Receita"):
        return jsonify({"ok": False, "msg": "Tipo inválido (Gasto ou Receita)."}), 400
    if not categoria or not descricao or not data_str:
        return jsonify({"ok": False, "msg": "Preencha categoria, descrição e data."}), 400

    try:
        d = parse_br_date(data_str)
    except:
        return jsonify({"ok": False, "msg": "Data inválida. Use dd/mm/aaaa."}), 400

    v = safe_float(valor)
    if v <= 0:
        return jsonify({"ok": False, "msg": "Valor inválido."}), 400

    new_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()

    ws.append_row([new_id, data_str, tipo, categoria, descricao, f"{v:.2f}", created_at])
    return jsonify({"ok": True, "msg": "Lançamento salvo!"})


@app.get("/ultimos")
def ultimos():
    ws = get_sheet()
    _, rows = get_rows_with_rownum(ws)

    month, year = get_month_year_from_request()
    q = request.args.get("q", default="", type=str)
    limit = request.args.get("limit", default=10, type=int)
    if limit < 1: limit = 10
    if limit > 200: limit = 200

    filtered = filter_rows(rows, month, year, q)

    # ordena por data + row (mais recente no fim)
    def sort_key(r):
        try:
            d = parse_br_date(r.get("Data", "01/01/1900"))
        except:
            d = date(1900,1,1)
        return (d, r.get("_row", 0))

    filtered.sort(key=sort_key)
    last = filtered[-limit:] if len(filtered) > limit else filtered
    return jsonify(last)


@app.get("/resumo")
def resumo():
    ws = get_sheet()
    _, rows = get_rows_with_rownum(ws)

    month, year = get_month_year_from_request()
    q = request.args.get("q", default="", type=str)

    filtered = filter_rows(rows, month, year, q)

    entradas = 0.0
    saidas = 0.0

    # séries por dia
    last_day = monthrange(year, month)[1]
    serie_receita = [0.0] * last_day
    serie_gasto = [0.0] * last_day

    # pizza por categoria
    pizza_gastos: Dict[str, float] = {}
    pizza_receitas: Dict[str, float] = {}

    for r in filtered:
        tipo = (r.get("Tipo") or "").strip()
        cat = (r.get("Categoria") or "Sem categoria").strip() or "Sem categoria"
        val = safe_float(r.get("Valor"))
        try:
            d = parse_br_date(r.get("Data"))
        except:
            continue
        di = d.day - 1

        if tipo == "Receita":
            entradas += val
            if 0 <= di < last_day:
                serie_receita[di] += val
            pizza_receitas[cat] = pizza_receitas.get(cat, 0.0) + val
        else:
            saidas += val
            if 0 <= di < last_day:
                serie_gasto[di] += val
            pizza_gastos[cat] = pizza_gastos.get(cat, 0.0) + val

    saldo = entradas - saidas

    # labels dos dias
    dias = [str(i+1).zfill(2) for i in range(last_day)]

    # ordena pizza (maior -> menor) e limita em 12 fatias (resto vira "Outros")
    def collapse_top(d: Dict[str, float], top_n=12):
        items = sorted(d.items(), key=lambda x: x[1], reverse=True)
        top = items[:top_n]
        rest = items[top_n:]
        if rest:
            top.append(("Outros", sum(v for _, v in rest)))
        labels = [k for k, _ in top]
        values = [round(v, 2) for _, v in top]
        return labels, values

    pg_l, pg_v = collapse_top(pizza_gastos)
    pr_l, pr_v = collapse_top(pizza_receitas)

    return jsonify({
        "month": month,
        "year": year,
        "entradas": round(entradas, 2),
        "saidas": round(saidas, 2),
        "saldo": round(saldo, 2),
        "dias": dias,
        "serie_receita": [round(x, 2) for x in serie_receita],
        "serie_gasto": [round(x, 2) for x in serie_gasto],
        "pizza_gastos_labels": pg_l,
        "pizza_gastos_values": pg_v,
        "pizza_receitas_labels": pr_l,
        "pizza_receitas_values": pr_v
    })


@app.patch("/lancamento/<int:row>")
def editar(row: int):
    ws = get_sheet()
    body = request.get_json(force=True, silent=True) or {}

    tipo = str(body.get("tipo", "")).strip()
    categoria = str(body.get("categoria", "")).strip()
    descricao = str(body.get("descricao", "")).strip()
    valor = body.get("valor", None)
    data_str = str(body.get("data", "")).strip()

    if tipo not in ("Gasto", "Receita"):
        return jsonify({"ok": False, "msg": "Tipo inválido (Gasto ou Receita)."}), 400
    if not categoria or not descricao or not data_str:
        return jsonify({"ok": False, "msg": "Preencha categoria, descrição e data."}), 400

    try:
        parse_br_date(data_str)
    except:
        return jsonify({"ok": False, "msg": "Data inválida. Use dd/mm/aaaa."}), 400

    v = safe_float(valor)
    if v <= 0:
        return jsonify({"ok": False, "msg": "Valor inválido."}), 400

    # colunas: A=ID B=Data C=Tipo D=Categoria E=Descrição F=Valor G=CreatedAt
    ws.update(f"B{row}", [[data_str]])
    ws.update(f"C{row}", [[tipo]])
    ws.update(f"D{row}", [[categoria]])
    ws.update(f"E{row}", [[descricao]])
    ws.update(f"F{row}", [[f"{v:.2f}"]])

    return jsonify({"ok": True, "msg": "Editado com sucesso!"})


@app.delete("/lancamento/<int:row>")
def deletar(row: int):
    ws = get_sheet()
    # cuidado para não deletar cabeçalho
    if row <= 1:
        return jsonify({"ok": False, "msg": "Linha inválida."}), 400
    ws.delete_rows(row)
    return jsonify({"ok": True, "msg": "Excluído com sucesso!"})


def build_filtered_for_export() -> List[Dict[str, Any]]:
    ws = get_sheet()
    _, rows = get_rows_with_rownum(ws)
    month, year = get_month_year_from_request()
    q = request.args.get("q", default="", type=str)
    filtered = filter_rows(rows, month, year, q)

    # ordena por data
    filtered.sort(key=lambda r: (parse_br_date(r.get("Data", "01/01/1900")), r.get("_row", 0)))
    return filtered


@app.get("/export.csv")
def export_csv():
    filtered = build_filtered_for_export()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Data", "Tipo", "Categoria", "Descrição", "Valor"])
    for r in filtered:
        writer.writerow([
            r.get("Data", ""),
            r.get("Tipo", ""),
            r.get("Categoria", ""),
            r.get("Descrição", ""),
            r.get("Valor", ""),
        ])

    data = output.getvalue().encode("utf-8-sig")
    return Response(
        data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=finance-ai.csv"}
    )


@app.get("/export.pdf")
def export_pdf():
    filtered = build_filtered_for_export()
    month, year = get_month_year_from_request()
    q = request.args.get("q", default="", type=str).strip()

    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    y = h - 40
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, f"Finance AI — Relatório {str(month).zfill(2)}/{year}")
    y -= 18

    c.setFont("Helvetica", 10)
    if q:
        c.drawString(40, y, f"Filtro (busca): {q}")
        y -= 16

    # totais
    entradas = 0.0
    saidas = 0.0
    for r in filtered:
        t = (r.get("Tipo") or "").strip()
        v = safe_float(r.get("Valor"))
        if t == "Receita":
            entradas += v
        else:
            saidas += v
    saldo = entradas - saidas

    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, f"Entradas: R$ {entradas:.2f}   Saídas: R$ {saidas:.2f}   Saldo: R$ {saldo:.2f}")
    y -= 18

    # tabela simples
    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Data")
    c.drawString(95, y, "Tipo")
    c.drawString(155, y, "Categoria")
    c.drawString(300, y, "Descrição")
    c.drawRightString(555, y, "Valor")
    y -= 10

    c.setLineWidth(0.5)
    c.line(40, y, 555, y)
    y -= 12

    c.setFont("Helvetica", 9)
    for r in filtered:
        if y < 60:
            c.showPage()
            y = h - 40
            c.setFont("Helvetica-Bold", 9)
            c.drawString(40, y, "Data")
            c.drawString(95, y, "Tipo")
            c.drawString(155, y, "Categoria")
            c.drawString(300, y, "Descrição")
            c.drawRightString(555, y, "Valor")
            y -= 10
            c.line(40, y, 555, y)
            y -= 12
            c.setFont("Helvetica", 9)

        data_str = (r.get("Data") or "")[:10]
        tipo = (r.get("Tipo") or "")[:10]
        cat = (r.get("Categoria") or "")[:22]
        desc = (r.get("Descrição") or "")[:38]
        val = safe_float(r.get("Valor"))

        c.drawString(40, y, data_str)
        c.drawString(95, y, tipo)
        c.drawString(155, y, cat)
        c.drawString(300, y, desc)
        c.drawRightString(555, y, f"R$ {val:.2f}")
        y -= 12

    c.showPage()
    c.save()

    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=finance-ai.pdf"}
    )


if __name__ == "__main__":
    # local dev
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=True)

