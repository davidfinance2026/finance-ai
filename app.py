import os
import io
import csv
import json
import datetime as dt
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import gspread
from flask import Flask, jsonify, request, session, send_file, render_template
from google.oauth2.service_account import Credentials
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

# PDF (export)
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


# =========================
# Config
# =========================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Planilha "master" (onde fica a aba Usuarios, e pode servir como fallback)
MASTER_SHEET_ID = os.getenv("USERS_SHEET_ID") or os.getenv("SHEET_ID")
USERS_TAB = os.getenv("USERS_TAB", "Usuarios")

# Fallback (se quiser manter um default)
DEFAULT_SHEET_ID = os.getenv("SHEET_ID")
DEFAULT_SHEET_TAB = os.getenv("SHEET_TAB", "Lancamentos")

FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev_secret_change_me")

# Se você quiser forçar cookie seguro em produção:
FORCE_SECURE_COOKIE = os.getenv("FORCE_SECURE_COOKIE", "1") == "1"


# =========================
# App
# =========================
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = FLASK_SECRET_KEY
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

if FORCE_SECURE_COOKIE:
    # Render usa HTTPS no domínio final. ProxyFix ajuda a detectar.
    app.config["SESSION_COOKIE_SECURE"] = True


# =========================
# Google Sheets client
# =========================
_client_cached: Optional[gspread.Client] = None


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

    creds_info = None

    env_json = os.getenv("SERVICE_ACCOUNT_JSON")
    if env_json:
        creds_info = json.loads(env_json)
    else:
        for path in ("/etc/secrets/google_creds.json", "google_creds.json"):
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    creds_info = json.load(f)
                break

    if not creds_info:
        raise RuntimeError(
            "Credenciais do Google não encontradas. Configure SERVICE_ACCOUNT_JSON "
            "ou /etc/secrets/google_creds.json"
        )

    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    _client_cached = gspread.authorize(creds)
    return _client_cached


# =========================
# Helpers: auth/session
# =========================
def require_login() -> Tuple[bool, Optional[Any]]:
    if not session.get("user_email") or not session.get("sheet_id") or not session.get("sheet_tab"):
        return False, (jsonify({"ok": False, "msg": "Não autenticado"}), 401)
    return True, None


def get_user_sheet_ctx() -> Tuple[str, str]:
    """
    Retorna (sheet_id, sheet_tab) do usuário logado.
    """
    sid = session.get("sheet_id")
    stab = session.get("sheet_tab")
    if not sid or not stab:
        # fallback (opcional)
        sid = DEFAULT_SHEET_ID
        stab = DEFAULT_SHEET_TAB
    return sid, stab


def now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def parse_iso_date(s: str) -> Optional[dt.date]:
    # Aceita "YYYY-MM-DD"
    try:
        y, m, d = [int(x) for x in s.split("-")]
        return dt.date(y, m, d)
    except Exception:
        return None


def parse_br_date(s: str) -> Optional[dt.date]:
    # Aceita "dd/mm/yyyy"
    try:
        d, m, y = [int(x) for x in s.split("/")]
        return dt.date(y, m, d)
    except Exception:
        return None


def money_to_float(v: Any) -> Optional[float]:
    # Lê valores vindos do Sheets (pode vir float, "360", "360,00", "R$ 360,00"...)
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    s = s.replace("R$", "").strip()

    has_comma = "," in s
    has_dot = "." in s

    if has_comma and has_dot:
        # decide pelo último separador
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")

    try:
        return float(s)
    except Exception:
        return None


def ensure_lancamentos_headers(ws: gspread.Worksheet) -> None:
    """
    Garante que a linha 1 tem cabeçalho padrão.
    """
    wanted = ["Tipo", "Categoria", "Descrição", "Valor", "Data", "CreatedAt"]
    first = ws.row_values(1)
    if [c.strip() for c in first[: len(wanted)]] != wanted:
        ws.update("A1:F1", [wanted])


def open_users_ws() -> gspread.Worksheet:
    if not MASTER_SHEET_ID:
        raise RuntimeError("MASTER_SHEET_ID/USERS_SHEET_ID não configurado.")
    sh = get_client().open_by_key(MASTER_SHEET_ID)
    return sh.worksheet(USERS_TAB)


def open_user_lancamentos_ws(sheet_id: str, sheet_tab: str) -> gspread.Worksheet:
    sh = get_client().open_by_key(sheet_id)
    ws = sh.worksheet(sheet_tab)
    ensure_lancamentos_headers(ws)
    return ws


def read_users() -> List[Dict[str, Any]]:
    """
    Lê aba Usuarios da planilha master.
    Colunas esperadas:
      Email | PasswordHash | Ativo | CreatedAt | SheetId | SheetTab
    """
    ws = open_users_ws()
    rows = ws.get_all_records()  # lista de dicts
    # Normaliza chaves possíveis
    out = []
    for r in rows:
        email = (r.get("Email") or r.get("email") or "").strip().lower()
        ph = (r.get("PasswordHash") or r.get("passwordhash") or r.get("Password") or "").strip()
        ativo = r.get("Ativo")
        created = r.get("CreatedAt") or r.get("createdat")
        sheet_id = (r.get("SheetId") or r.get("SheetID") or r.get("sheet_id") or "").strip()
        sheet_tab = (r.get("SheetTab") or r.get("sheet_tab") or "").strip() or DEFAULT_SHEET_TAB

        # Ativo pode vir como TRUE/FALSE string
        ativo_bool = True
        if isinstance(ativo, bool):
            ativo_bool = ativo
        elif ativo is None or str(ativo).strip() == "":
            ativo_bool = True
        else:
            ativo_bool = str(ativo).strip().lower() in ("true", "1", "yes", "sim")

        out.append(
            {
                "Email": email,
                "PasswordHash": ph,
                "Ativo": ativo_bool,
                "CreatedAt": created,
                "SheetId": sheet_id,
                "SheetTab": sheet_tab,
            }
        )
    return out


def find_user(email: str) -> Optional[Dict[str, Any]]:
    email = (email or "").strip().lower()
    for u in read_users():
        if u["Email"] == email:
            return u
    return None


# =========================
# Routes: pages
# =========================
@app.get("/")
def home():
    # Seu HTML geralmente está em templates/index.html
    # Se você usa outro nome, ajuste aqui.
    return render_template("index.html")


# =========================
# Routes: auth
# =========================
@app.get("/me")
def me():
    if session.get("user_email"):
        return jsonify({"ok": True, "email": session.get("user_email")})
    return jsonify({"ok": False}), 401


@app.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()

    if not email or not password:
        return jsonify({"ok": False, "msg": "Informe e-mail e senha."}), 400

    u = find_user(email)
    if not u:
        return jsonify({"ok": False, "msg": "Usuário não encontrado."}), 401
    if not u.get("Ativo", True):
        return jsonify({"ok": False, "msg": "Usuário desativado."}), 403

    ph = u.get("PasswordHash") or ""
    if not ph or not check_password_hash(ph, password):
        return jsonify({"ok": False, "msg": "Credenciais inválidas."}), 401

    # SheetId/SheetTab obrigatórios na opção 3 (mas deixo fallback)
    sheet_id = u.get("SheetId") or DEFAULT_SHEET_ID
    sheet_tab = u.get("SheetTab") or DEFAULT_SHEET_TAB

    if not sheet_id:
        return jsonify({"ok": False, "msg": "Usuário sem SheetId configurado."}), 500

    session["user_email"] = email
    session["sheet_id"] = sheet_id
    session["sheet_tab"] = sheet_tab

    return jsonify({"ok": True})


@app.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


# =========================
# Core: read/filter sheet rows
# =========================
def get_all_lancamentos(sheet_id: str, sheet_tab: str) -> List[Dict[str, Any]]:
    ws = open_user_lancamentos_ws(sheet_id, sheet_tab)
    rows = ws.get_all_records()  # dicts com as colunas
    # Precisamos do número da linha (_row)
    # get_all_records ignora header e começa na linha 2
    # então linha real = index + 2
    out = []
    for i, r in enumerate(rows, start=2):
        item = dict(r)
        item["_row"] = i
        out.append(item)
    return out


def apply_filters(items: List[Dict[str, Any]], args: Dict[str, str]) -> List[Dict[str, Any]]:
    # month/year (default obrigatório no front)
    month = int(args.get("month") or 0) or None
    year = int(args.get("year") or 0) or None

    tipo = (args.get("tipo") or "Todos").strip()
    q = (args.get("q") or "").strip().lower()
    order = (args.get("order") or "recent").strip()

    date_from = (args.get("date_from") or "").strip()
    date_to = (args.get("date_to") or "").strip()
    value_min = (args.get("value_min") or "").strip()
    value_max = (args.get("value_max") or "").strip()

    df = parse_iso_date(date_from) if date_from else None
    dt_ = parse_iso_date(date_to) if date_to else None
    vmin = money_to_float(value_min) if value_min else None
    vmax = money_to_float(value_max) if value_max else None

    def item_date(it: Dict[str, Any]) -> Optional[dt.date]:
        # Data vem "dd/mm/yyyy"
        return parse_br_date(str(it.get("Data") or "").strip())

    def item_value(it: Dict[str, Any]) -> Optional[float]:
        return money_to_float(it.get("Valor"))

    filtered = []
    for it in items:
        t = str(it.get("Tipo") or "").strip()
        cat = str(it.get("Categoria") or "").strip()
        desc = str(it.get("Descrição") or "").strip()
        dbr = str(it.get("Data") or "").strip()

        d = item_date(it)
        v = item_value(it) or 0.0

        # month/year (se não houver data válida, ignora)
        if month and year and d:
            if d.month != month or d.year != year:
                continue

        # tipo
        if tipo != "Todos":
            if t.lower() != tipo.lower():
                continue

        # busca
        if q:
            hay = f"{t} {cat} {desc} {dbr}".lower()
            if q not in hay:
                continue

        # date range
        if df and d and d < df:
            continue
        if dt_ and d and d > dt_:
            continue

        # value range
        if vmin is not None and v < vmin:
            continue
        if vmax is not None and v > vmax:
            continue

        filtered.append(it)

    # sort
    def sort_key_date(it):
        d = parse_br_date(str(it.get("Data") or "").strip())
        # se não tiver data, vai pro fim
        return d or dt.date(1900, 1, 1)

    def sort_key_value(it):
        return money_to_float(it.get("Valor")) or 0.0

    if order == "oldest":
        filtered.sort(key=sort_key_date)
    elif order == "value_desc":
        filtered.sort(key=sort_key_value, reverse=True)
    elif order == "value_asc":
        filtered.sort(key=sort_key_value)
    else:  # recent
        filtered.sort(key=sort_key_date, reverse=True)

    return filtered


# =========================
# API: lançar
# =========================
@app.post("/lancar")
def lancar():
    ok, resp = require_login()
    if not ok:
        return resp

    data = request.get_json(silent=True) or {}
    tipo = (data.get("tipo") or "").strip()
    categoria = (data.get("categoria") or "").strip()
    descricao = (data.get("descricao") or "").strip()
    valor = data.get("valor")
    data_br = (data.get("data") or "").strip()  # dd/mm/yyyy

    if tipo not in ("Gasto", "Receita"):
        return jsonify({"ok": False, "msg": "Tipo inválido."}), 400
    if not categoria or not descricao:
        return jsonify({"ok": False, "msg": "Categoria e descrição são obrigatórias."}), 400

    v = money_to_float(valor)
    if v is None or v <= 0:
        return jsonify({"ok": False, "msg": "Valor inválido."}), 400

    if data_br:
        if not parse_br_date(data_br):
            return jsonify({"ok": False, "msg": "Data inválida (use dd/mm/aaaa)."}), 400
    else:
        # se não veio data, usa hoje
        today = dt.date.today()
        data_br = f"{today.day:02d}/{today.month:02d}/{today.year}"

    sheet_id, sheet_tab = get_user_sheet_ctx()
    ws = open_user_lancamentos_ws(sheet_id, sheet_tab)

    ws.append_row(
        [tipo, categoria, descricao, v, data_br, now_iso()],
        value_input_option="USER_ENTERED",
    )

    return jsonify({"ok": True})


# =========================
# API: ultimos (paginado)
# =========================
@app.get("/ultimos")
def ultimos():
    ok, resp = require_login()
    if not ok:
        return resp

    sheet_id, sheet_tab = get_user_sheet_ctx()
    items = get_all_lancamentos(sheet_id, sheet_tab)

    filtered = apply_filters(items, request.args.to_dict(flat=True))
    total = len(filtered)

    page = int(request.args.get("page") or 1)
    limit = int(request.args.get("limit") or 10)
    page = max(1, page)
    limit = max(1, min(500, limit))

    start = (page - 1) * limit
    end = start + limit
    paged = filtered[start:end]

    return jsonify({"ok": True, "total": total, "items": paged})


# =========================
# API: resumo (cards + gráficos)
# =========================
@app.get("/resumo")
def resumo():
    ok, resp = require_login()
    if not ok:
        return resp

    args = request.args.to_dict(flat=True)
    month = int(args.get("month") or 0) or dt.date.today().month
    year = int(args.get("year") or 0) or dt.date.today().year

    sheet_id, sheet_tab = get_user_sheet_ctx()
    items = get_all_lancamentos(sheet_id, sheet_tab)
    filtered = apply_filters(items, args)

    entradas = 0.0
    saidas = 0.0

    # série por dia do mês (do month/year atual)
    last_day = (dt.date(year + (month // 12), (month % 12) + 1, 1) - dt.timedelta(days=1)).day
    dias_labels = [f"{d:02d}" for d in range(1, last_day + 1)]
    serie_receita = [0.0] * last_day
    serie_gasto = [0.0] * last_day

    # categorias
    gastos_cat: Dict[str, float] = {}
    receitas_cat: Dict[str, float] = {}

    for it in filtered:
        t = str(it.get("Tipo") or "").strip()
        cat = str(it.get("Categoria") or "Sem categoria").strip() or "Sem categoria"
        v = money_to_float(it.get("Valor")) or 0.0
        d = parse_br_date(str(it.get("Data") or "").strip())

        if t.lower() == "receita":
            entradas += v
            receitas_cat[cat] = receitas_cat.get(cat, 0.0) + v
            if d and d.month == month and d.year == year:
                serie_receita[d.day - 1] += v
        else:
            saidas += v
            gastos_cat[cat] = gastos_cat.get(cat, 0.0) + v
            if d and d.month == month and d.year == year:
                serie_gasto[d.day - 1] += v

    saldo = entradas - saidas

    def top_pairs(dct: Dict[str, float]) -> List[Dict[str, Any]]:
        arr = [{"categoria": k, "total": v} for k, v in dct.items()]
        arr.sort(key=lambda x: x["total"], reverse=True)
        return arr

    gastos_categorias = top_pairs(gastos_cat)
    receitas_categorias = top_pairs(receitas_cat)

    return jsonify(
        {
            "ok": True,
            "entradas": entradas,
            "saidas": saidas,
            "saldo": saldo,
            "dias": dias_labels,
            "serie_receita": serie_receita,
            "serie_gasto": serie_gasto,
            "pizza_gastos_labels": [x["categoria"] for x in gastos_categorias],
            "pizza_gastos_values": [x["total"] for x in gastos_categorias],
            "pizza_receitas_labels": [x["categoria"] for x in receitas_categorias],
            "pizza_receitas_values": [x["total"] for x in receitas_categorias],
            "gastos_categorias": gastos_categorias,
            "receitas_categorias": receitas_categorias,
        }
    )


# =========================
# API: editar/excluir
# =========================
@app.patch("/lancamento/<int:row>")
def editar(row: int):
    ok, resp = require_login()
    if not ok:
        return resp

    if row < 2:
        return jsonify({"ok": False, "msg": "Linha inválida."}), 400

    data = request.get_json(silent=True) or {}
    tipo = (data.get("tipo") or "").strip()
    categoria = (data.get("categoria") or "").strip()
    descricao = (data.get("descricao") or "").strip()
    valor = data.get("valor")
    data_br = (data.get("data") or "").strip()

    if tipo not in ("Gasto", "Receita"):
        return jsonify({"ok": False, "msg": "Tipo inválido."}), 400
    if not categoria or not descricao:
        return jsonify({"ok": False, "msg": "Categoria e descrição são obrigatórias."}), 400
    v = money_to_float(valor)
    if v is None or v <= 0:
        return jsonify({"ok": False, "msg": "Valor inválido."}), 400
    if not data_br or not parse_br_date(data_br):
        return jsonify({"ok": False, "msg": "Data inválida (use dd/mm/aaaa)."}), 400

    sheet_id, sheet_tab = get_user_sheet_ctx()
    ws = open_user_lancamentos_ws(sheet_id, sheet_tab)

    # Atualiza colunas A-E (mantém CreatedAt)
    ws.update(f"A{row}:E{row}", [[tipo, categoria, descricao, v, data_br]], value_input_option="USER_ENTERED")
    return jsonify({"ok": True})


@app.delete("/lancamento/<int:row>")
def excluir(row: int):
    ok, resp = require_login()
    if not ok:
        return resp

    if row < 2:
        return jsonify({"ok": False, "msg": "Linha inválida."}), 400

    sheet_id, sheet_tab = get_user_sheet_ctx()
    ws = open_user_lancamentos_ws(sheet_id, sheet_tab)
    ws.delete_rows(row)
    return jsonify({"ok": True})


# =========================
# Export CSV/PDF (filtrado)
# =========================
def filtered_items_for_export() -> List[Dict[str, Any]]:
    sheet_id, sheet_tab = get_user_sheet_ctx()
    items = get_all_lancamentos(sheet_id, sheet_tab)
    return apply_filters(items, request.args.to_dict(flat=True))


@app.get("/export.csv")
def export_csv():
    ok, resp = require_login()
    if not ok:
        return resp

    arr = filtered_items_for_export()

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Tipo", "Categoria", "Descrição", "Valor", "Data"])
    for it in arr:
        w.writerow([
            it.get("Tipo", ""),
            it.get("Categoria", ""),
            it.get("Descrição", ""),
            it.get("Valor", ""),
            it.get("Data", ""),
        ])

    data = buf.getvalue().encode("utf-8-sig")
    return send_file(
        io.BytesIO(data),
        mimetype="text/csv",
        as_attachment=True,
        download_name="finance-ai.csv",
    )


@app.get("/export.pdf")
def export_pdf():
    ok, resp = require_login()
    if not ok:
        return resp

    arr = filtered_items_for_export()

    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4)
    width, height = A4

    margin = 12 * mm
    y = height - margin

    c.setFont("Helvetica-Bold", 14)
    c.drawString(margin, y, "Finance AI — Exportação")
    y -= 10 * mm

    c.setFont("Helvetica", 10)
    c.drawString(margin, y, f"Gerado em: {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}")
    y -= 10 * mm

    # Cabeçalho
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin, y, "Tipo")
    c.drawString(margin + 28*mm, y, "Categoria")
    c.drawString(margin + 78*mm, y, "Descrição")
    c.drawString(margin + 150*mm, y, "Valor")
    c.drawString(margin + 175*mm, y, "Data")
    y -= 6 * mm

    c.setFont("Helvetica", 9)

    def new_page():
        nonlocal y
        c.showPage()
        y = height - margin
        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin, y, "Tipo")
        c.drawString(margin + 28*mm, y, "Categoria")
        c.drawString(margin + 78*mm, y, "Descrição")
        c.drawString(margin + 150*mm, y, "Valor")
        c.drawString(margin + 175*mm, y, "Data")
        y -= 6 * mm
        c.setFont("Helvetica", 9)

    for it in arr:
        if y < margin + 12*mm:
            new_page()

        tipo = str(it.get("Tipo", ""))[:12]
        cat = str(it.get("Categoria", ""))[:22]
        desc = str(it.get("Descrição", ""))[:36]
        valor = str(it.get("Valor", ""))
        data_ = str(it.get("Data", ""))

        c.drawString(margin, y, tipo)
        c.drawString(margin + 28*mm, y, cat)
        c.drawString(margin + 78*mm, y, desc)
        c.drawRightString(margin + 170*mm, y, valor)
        c.drawString(margin + 175*mm, y, data_)

        y -= 5 * mm

    c.save()
    out.seek(0)

    return send_file(
        out,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="finance-ai.pdf",
    )


# =========================
# Run
# =========================
if __name__ == "__main__":
    # Render usa gunicorn, mas localmente você pode rodar:
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
