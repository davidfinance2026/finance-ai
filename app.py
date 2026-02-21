import os
import json
import uuid
import csv
import io
from datetime import datetime, date
from calendar import monthrange
from typing import Any, Dict, List, Tuple, Optional

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

_client_cached: Optional[gspread.Client] = None


# =========================
# AUTH
# =========================
def require_auth():
    """Protege as rotas de API usando X-APP-TOKEN == APP_PASSWORD (se existir)."""
    pwd = os.getenv("APP_PASSWORD", "").strip()
    if not pwd:
        return  # sem senha -> sem auth

    token = request.headers.get("X-APP-TOKEN", "").strip()
    if token != pwd:
        abort(401)


@app.before_request
def _auth_middleware():
    # Home e login ficam livres para o app carregar.
    # Protege API e exports.
    public_paths = {"/", "/login"}
    if request.path in public_paths:
        return
    # se você tiver /static, também libera:
    if request.path.startswith("/static"):
        return
    require_auth()


# =========================
# GOOGLE SHEETS
# =========================
def get_client() -> gspread.Client:
    """
    Prioridade:
    1) SERVICE_ACCOUNT_JSON (env com JSON inteiro)
    2) Secret File no Render em /etc/secrets/google_creds.json
    3) arquivo local google_creds.json
    """
    global _client_cached
    if _client_cached is not None:
        return _client_cached

    raw = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        info = json.loads(raw)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        _client_cached = gspread.authorize(creds)
        return _client_cached

    secret_path = "/etc/secrets/google_creds.json"
    if os.path.exists(secret_path):
        creds = Credentials.from_service_account_file(secret_path, scopes=SCOPES)
        _client_cached = gspread.authorize(creds)
        return _client_cached

    local_path = "google_creds.json"
    if os.path.exists(local_path):
        creds = Credentials.from_service_account_file(local_path, scopes=SCOPES)
        _client_cached = gspread.authorize(creds)
        return _client_cached

    raise RuntimeError(
        "Credenciais não encontradas. "
        "Defina SERVICE_ACCOUNT_JSON ou envie Secret File google_creds.json no Render."
    )


def ensure_headers(ws: gspread.Worksheet):
    """
    Garante que a linha 1 tenha o header correto.
    Não apaga dados: só ajusta a linha 1.
    """
    values = ws.get_all_values()
    if not values:
        ws.append_row(HEADERS)
        return

    first = [c.strip() for c in (values[0] or [])]
    if first != HEADERS:
        # Atualiza apenas A1:G1
        ws.update("A1", [HEADERS])


def get_sheet() -> gspread.Worksheet:
    """
    Usa:
    - SHEET_ID (obrigatório)
    - SHEET_TAB (nome da aba, ex: Lancamentos) (opcional)
    """
    sheet_id = os.getenv("SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("Missing env var SHEET_ID")

    client = get_client()
    sh = client.open_by_key(sheet_id)

    ws_name = os.getenv("SHEET_TAB", "").strip()
    ws = sh.worksheet(ws_name) if ws_name else sh.get_worksheet(0)

    ensure_headers(ws)
    return ws


# =========================
# PARSERS
# =========================
def parse_br_date(s: str) -> date:
    return datetime.strptime(s.strip(), "%d/%m/%Y").date()


def safe_float(v: Any) -> float:
    """
    Aceita:
    - número (int/float)
    - "360,00"
    - "360.00"
    - "1.234,56"
    - "1,234.56"
    """
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)

    s = str(v).strip()
    if not s:
        return 0.0

    s = s.replace("R$", "").strip()

    has_comma = "," in s
    has_dot = "." in s

    if has_comma and has_dot:
        # decide pelo último separador
        if s.rfind(",") > s.rfind("."):
            # BR: 1.234,56 -> remove pontos, troca vírgula por ponto
            s = s.replace(".", "").replace(",", ".")
        else:
            # US: 1,234.56 -> remove vírgulas
            s = s.replace(",", "")
    elif has_comma:
        # 1234,56 -> troca vírgula por ponto; remove pontos (milhar)
        s = s.replace(".", "").replace(",", ".")
    else:
        # só ponto (ou nada). Se vier "360.00" é ok.
        # Se vier "1.234" pode ser milhar — mas sem vírgula é ambíguo.
        # Vamos manter como número normal:
        s = s.replace(",", "")

    try:
        return float(s)
    except:
        return 0.0


# =========================
# ROWS
# =========================
def get_rows_with_rownum(ws: gspread.Worksheet) -> Tuple[List[str], List[Dict[str, Any]]]:
    """
    Retorna headers e lista de dicts com _row (número da linha real no Sheets).
    """
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return HEADERS, []

    headers = values[0]
    data_rows = values[1:]

    out: List[Dict[str, Any]] = []
    for idx, row in enumerate(data_rows, start=2):
        obj: Dict[str, Any] = {}
        for h_i, h in enumerate(headers):
            obj[h] = row[h_i] if h_i < len(row) else ""
        obj["_row"] = idx
        out.append(obj)

    return headers, out


def get_month_year_from_request() -> Tuple[int, int]:
    today = datetime.now().date()
    month = request.args.get("month", default=today.month, type=int)
    year = request.args.get("year", default=today.year, type=int)
    if month < 1 or month > 12:
        month = today.month
    if year < 1900:
        year = today.year
    return month, year


def get_tipo_filter() -> str:
    """
    tipo=Todos|Gasto|Receita
    """
    t = (request.args.get("tipo", default="Todos", type=str) or "Todos").strip()
    if t not in ("Todos", "Gasto", "Receita"):
        t = "Todos"
    return t


def filter_rows(
    rows: List[Dict[str, Any]],
    month: int,
    year: int,
    q: str,
    tipo_filter: str
) -> List[Dict[str, Any]]:
    q = (q or "").strip().lower()

    filtered: List[Dict[str, Any]] = []
    for r in rows:
        d_str = (r.get("Data") or "").strip()
        try:
            d = parse_br_date(d_str)
        except:
            continue

        if d.month != month or d.year != year:
            continue

        tipo = (r.get("Tipo") or "").strip()
        if tipo_filter != "Todos" and tipo != tipo_filter:
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


def sort_key_desc(r: Dict[str, Any]):
    try:
        d = parse_br_date(r.get("Data", "01/01/1900"))
    except:
        d = date(1900, 1, 1)
    rownum = int(r.get("_row", 0) or 0)
    return (d, rownum)


# =========================
# ROUTES
# =========================
@app.get("/")
def home():
    return render_template("index.html")


@app.post("/login")
def login():
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

    new_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()

    # Valor com 2 casas, ponto decimal (estável)
    ws.append_row([new_id, data_str, tipo, categoria, descricao, f"{v:.2f}", created_at])
    return jsonify({"ok": True, "msg": "Lançamento salvo!"})


@app.get("/ultimos")
def ultimos():
    ws = get_sheet()
    _, rows = get_rows_with_rownum(ws)

    month, year = get_month_year_from_request()
    q = request.args.get("q", default="", type=str)
    tipo_filter = get_tipo_filter()

    limit = request.args.get("limit", default=10, type=int)
    page = request.args.get("page", default=1, type=int)
    if limit < 1: limit = 10
    if limit > 200: limit = 200
    if page < 1: page = 1

    filtered = filter_rows(rows, month, year, q, tipo_filter)

    # ordena do mais recente pro mais antigo
    filtered.sort(key=sort_key_desc, reverse=True)

    total = len(filtered)
    start = (page - 1) * limit
    end = start + limit
    items = filtered[start:end]

    return jsonify({
        "items": items,
        "total": total,
        "page": page,
        "limit": limit
    })


@app.get("/resumo")
def resumo():
    ws = get_sheet()
    _, rows = get_rows_with_rownum(ws)

    month, year = get_month_year_from_request()
    q = request.args.get("q", default="", type=str)
    tipo_filter = get_tipo_filter()

    filtered = filter_rows(rows, month, year, q, tipo_filter)

    entradas = 0.0
    saidas = 0.0

    last_day = monthrange(year, month)[1]
    serie_receita = [0.0] * last_day
    serie_gasto = [0.0] * last_day

    pizza_gastos: Dict[str, float] = {}
    pizza_receitas: Dict[str, float] = {}

    for r in filtered:
        tipo = (r.get("Tipo") or "").strip()
        cat = (r.get("Categoria") or "Sem categoria").strip() or "Sem categoria"
        val = safe_float(r.get("Valor"))

        try:
            d = parse_br_date(r.get("Data", ""))
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
    dias = [str(i + 1).zfill(2) for i in range(last_day)]

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
        "tipo": tipo_filter,
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

    if row <= 1:
        return jsonify({"ok": False, "msg": "Linha inválida."}), 400

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

    ws.update(f"B{row}", [[data_str]])
    ws.update(f"C{row}", [[tipo]])
    ws.update(f"D{row}", [[categoria]])
    ws.update(f"E{row}", [[descricao]])
    ws.update(f"F{row}", [[f"{v:.2f}"]])

    return jsonify({"ok": True, "msg": "Editado com sucesso!"})


@app.delete("/lancamento/<int:row>")
def deletar(row: int):
    ws = get_sheet()
    if row <= 1:
        return jsonify({"ok": False, "msg": "Linha inválida."}), 400
    ws.delete_rows(row)
    return jsonify({"ok": True, "msg": "Excluído com sucesso!"})


def build_filtered_for_export() -> List[Dict[str, Any]]:
    ws = get_sheet()
    _, rows = get_rows_with_rownum(ws)

    month, year = get_month_year_from_request()
    q = request.args.get("q", default="", type=str)
    tipo_filter = get_tipo_filter()

    filtered = filter_rows(rows, month, year, q, tipo_filter)
    filtered.sort(key=sort_key_desc, reverse=True)
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
    tipo_filter = get_tipo_filter()

    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    y = h - 40
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, f"Finance AI — Relatório {str(month).zfill(2)}/{year} — {tipo_filter}")
    y -= 18

    c.setFont("Helvetica", 10)
    if q:
        c.drawString(40, y, f"Busca: {q}")
        y -= 16

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
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=True)
