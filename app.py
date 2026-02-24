# app.py
import os
import csv
import io
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

from flask import (
    Flask, request, jsonify, session, render_template,
    send_from_directory, make_response
)

import gspread
from google.oauth2.service_account import Credentials
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix


# =========================
# CONFIG
# =========================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = os.getenv("SHEET_ID", "").strip()
SHEET_TAB = os.getenv("SHEET_TAB", "Lancamentos").strip()
USERS_TAB = os.getenv("USERS_TAB", "Usuarios").strip()
METAS_TAB = os.getenv("METAS_TAB", "Metas").strip()

FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret").strip()

# Admin bootstrap via env
APP_EMAIL = os.getenv("APP_EMAIL", "").strip()
APP_PASSWORD_HASH = os.getenv("APP_PASSWORD_HASH", "").strip()

# credentials: 1) JSON env 2) secret file 3) local file
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()
SECRET_FILE_PATH = "/etc/secrets/google_creds.json"

# IMPORTANT: in localhost (http), secure cookie breaks sessions
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1").strip().lower() in ("1", "true", "sim", "yes")

_client_cached: Optional[gspread.Client] = None

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = FLASK_SECRET_KEY

# Render/proxy support (fix session cookie behind HTTPS proxy)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
)


# =========================
# GOOGLE CLIENT
# =========================
def get_client() -> gspread.Client:
    global _client_cached
    if _client_cached:
        return _client_cached

    if SERVICE_ACCOUNT_JSON:
        import json
        info = json.loads(SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    elif os.path.exists(SECRET_FILE_PATH):
        creds = Credentials.from_service_account_file(SECRET_FILE_PATH, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("google_creds.json", scopes=SCOPES)

    _client_cached = gspread.authorize(creds)
    return _client_cached


def open_spread():
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID não configurado.")
    return get_client().open_by_key(SHEET_ID)


def ensure_worksheet(spread, title: str, headers: List[str]):
    try:
        ws = spread.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spread.add_worksheet(title=title, rows=2000, cols=max(10, len(headers)))

    existing = ws.row_values(1)
    if [h.strip() for h in existing] != headers:
        ws.clear()
        ws.append_row(headers, value_input_option="RAW")
    return ws


# =========================
# HELPERS
# =========================
def now_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def safe_lower(x: Any) -> str:
    return str(x or "").strip().lower()


def is_admin_email(email: str) -> bool:
    if not APP_EMAIL:
        return False
    return safe_lower(email) == safe_lower(APP_EMAIL)


def require_login() -> Optional[Tuple[Dict[str, Any], int]]:
    if not session.get("user_email"):
        return {"ok": False, "msg": "Não autenticado"}, 401
    return None


def parse_date_br(s: str) -> Optional[date]:
    try:
        d, m, y = s.strip().split("/")
        return date(int(y), int(m), int(d))
    except Exception:
        return None


def money_to_float(v: Any) -> float:
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
        # pt-BR: 1.234,56
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
        return 0.0


def to_int(v: Any, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default


def current_month_year() -> Tuple[int, int]:
    today = date.today()
    return today.month, today.year


def get_records(ws) -> List[Dict[str, Any]]:
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return []
    headers = values[0]
    out = []
    for i, row in enumerate(values[1:], start=2):
        item = {headers[j]: row[j] if j < len(row) else "" for j in range(len(headers))}
        item["_row"] = i
        out.append(item)
    return out


def match_query(item: Dict[str, Any], q: str) -> bool:
    if not q:
        return True
    ql = q.lower()
    hay = " ".join([
        str(item.get("Tipo", "")),
        str(item.get("Categoria", "")),
        str(item.get("Descrição", "")),
        str(item.get("Data", "")),
        str(item.get("Valor", "")),
    ]).lower()
    return ql in hay


def item_value_num(item: Dict[str, Any]) -> float:
    return money_to_float(item.get("Valor", 0))


def item_date_num(item: Dict[str, Any]) -> int:
    d = parse_date_br(str(item.get("Data", "")))
    if not d:
        return 0
    return int(d.strftime("%Y%m%d"))


# =========================
# STATIC / PWA HEADERS (no duplicate /static route)
# =========================
@app.after_request
def add_pwa_headers(resp):
    path = request.path or ""

    # root service worker: always fresh check
    if path == "/sw.js":
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
        return resp

    # manifest MIME
    if path.endswith("/static/manifest.json"):
        resp.headers["Content-Type"] = "application/manifest+json; charset=utf-8"

    # JS MIME (covers /static/vendor/chart.umd.min.js too)
    if path.endswith(".js"):
        resp.headers.setdefault("Content-Type", "application/javascript; charset=utf-8")

    # JSON MIME
    if path.endswith(".json"):
        resp.headers.setdefault("Content-Type", "application/json; charset=utf-8")

    return resp


# ✅ route /sw.js at root (best practice for PWA scope)
@app.route("/sw.js")
def sw_root():
    return send_from_directory(app.static_folder, "sw.js")


# =========================
# PAGES
# =========================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"ok": True})


# =========================
# WORKSHEETS
# =========================
def users_ws():
    spread = open_spread()
    return ensure_worksheet(
        spread,
        USERS_TAB,
        headers=["Email", "PasswordHash", "Ativo", "CreatedAt"]
    )


def lanc_ws():
    spread = open_spread()
    return ensure_worksheet(
        spread,
        SHEET_TAB,
        headers=["Email", "Tipo", "Categoria", "Descrição", "Valor", "Data", "CreatedAt"]
    )


def metas_ws():
    spread = open_spread()
    return ensure_worksheet(
        spread,
        METAS_TAB,
        headers=["Email", "Mes", "Ano", "MetaReceitas", "MetaGastos", "CreatedAt", "UpdatedAt"]
    )


def ensure_admin_bootstrap():
    if not APP_EMAIL or not APP_PASSWORD_HASH:
        return
    ws = users_ws()
    recs = get_records(ws)
    for r in recs:
        if safe_lower(r.get("Email")) == safe_lower(APP_EMAIL):
            return
    ws.append_row([APP_EMAIL, APP_PASSWORD_HASH, "1", now_str()], value_input_option="RAW")


# =========================
# AUTH
# =========================
@app.route("/me")
def me():
    email = session.get("user_email")
    if email:
        return jsonify({"ok": True, "email": email, "is_admin": is_admin_email(email)})
    return jsonify({"ok": False}), 401


@app.route("/login", methods=["POST"])
def login():
    ensure_admin_bootstrap()

    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", "")).strip()

    if not email or not password:
        return jsonify({"ok": False, "msg": "Informe e-mail e senha"}), 400

    ws = users_ws()
    recs = get_records(ws)

    user = None
    for r in recs:
        if safe_lower(r.get("Email")) == email:
            user = r
            break

    if not user:
        return jsonify({"ok": False, "msg": "Credenciais inválidas"}), 401

    ativo = str(user.get("Ativo", "1")).strip()
    if ativo not in ("1", "true", "True", "SIM", "sim"):
        return jsonify({"ok": False, "msg": "Usuário inativo"}), 403

    ph = str(user.get("PasswordHash", "")).strip()
    if not ph or not check_password_hash(ph, password):
        return jsonify({"ok": False, "msg": "Credenciais inválidas"}), 401

    session["user_email"] = email
    return jsonify({"ok": True, "email": email, "is_admin": is_admin_email(email)})


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user_email", None)
    return jsonify({"ok": True})


# =========================
# CREATE USER (ADMIN)
# =========================
def _create_user_impl():
    guard = require_login()
    if guard:
        return jsonify(guard[0]), guard[1]

    if not is_admin_email(session.get("user_email", "")):
        return jsonify({"ok": False, "msg": "Apenas admin"}), 403

    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", "")).strip()

    if not email or not password:
        return jsonify({"ok": False, "msg": "Informe email e senha"}), 400

    ws = users_ws()
    recs = get_records(ws)
    for r in recs:
        if safe_lower(r.get("Email")) == email:
            return jsonify({"ok": False, "msg": "Usuário já existe"}), 400

    ph = generate_password_hash(password, method="pbkdf2:sha256", salt_length=16)
    ws.append_row([email, ph, "1", now_str()], value_input_option="RAW")
    return jsonify({"ok": True, "msg": "Usuário criado com sucesso"})


@app.route("/create_user", methods=["POST"])
def create_user():
    return _create_user_impl()


@app.route("/admin/create_user", methods=["POST"])
def admin_create_user():
    return _create_user_impl()


# =========================
# METAS
# =========================
def _find_meta_row(ws, email: str, mes: int, ano: int) -> Optional[int]:
    recs = get_records(ws)
    for r in recs:
        if safe_lower(r.get("Email")) != safe_lower(email):
            continue
        if to_int(r.get("Mes"), -1) == mes and to_int(r.get("Ano"), -1) == ano:
            return int(r.get("_row", 0)) or None
    return None


def _get_meta(email: str, mes: int, ano: int) -> Dict[str, Any]:
    ws = metas_ws()
    recs = get_records(ws)
    for r in recs:
        if safe_lower(r.get("Email")) == safe_lower(email) and to_int(r.get("Mes"), -1) == mes and to_int(r.get("Ano"), -1) == ano:
            return {
                "mes": mes,
                "ano": ano,
                "meta_receitas": money_to_float(r.get("MetaReceitas")),
                "meta_gastos": money_to_float(r.get("MetaGastos")),
            }
    return {"mes": mes, "ano": ano, "meta_receitas": 0.0, "meta_gastos": 0.0}


@app.route("/metas", methods=["GET"])
def metas_get():
    guard = require_login()
    if guard:
        return jsonify(guard[0]), guard[1]

    email = session["user_email"]
    mes = request.args.get("month", type=int)
    ano = request.args.get("year", type=int)
    if not mes or not ano:
        mes, ano = current_month_year()

    meta = _get_meta(email, int(mes), int(ano))
    return jsonify({"ok": True, **meta})


@app.route("/metas", methods=["POST"])
def metas_set():
    guard = require_login()
    if guard:
        return jsonify(guard[0]), guard[1]

    email = session["user_email"]
    dataj = request.get_json(silent=True) or {}

    mes = to_int(dataj.get("month"), 0)
    ano = to_int(dataj.get("year"), 0)
    if mes <= 0 or ano <= 0:
        mes, ano = current_month_year()

    meta_receitas = money_to_float(dataj.get("meta_receitas"))
    meta_gastos = money_to_float(dataj.get("meta_gastos"))
    if meta_receitas < 0 or meta_gastos < 0:
        return jsonify({"ok": False, "msg": "Metas não podem ser negativas"}), 400

    ws = metas_ws()
    headers = ws.row_values(1)
    col = {h: idx + 1 for idx, h in enumerate(headers)}

    row = _find_meta_row(ws, email, mes, ano)
    if row:
        ws.update_cell(row, col["MetaReceitas"], f"{meta_receitas:.2f}")
        ws.update_cell(row, col["MetaGastos"], f"{meta_gastos:.2f}")
        ws.update_cell(row, col["UpdatedAt"], now_str())
    else:
        ws.append_row(
            [email, str(mes), str(ano), f"{meta_receitas:.2f}", f"{meta_gastos:.2f}", now_str(), now_str()],
            value_input_option="RAW"
        )

    return jsonify({"ok": True, "msg": "Metas salvas", "month": mes, "year": ano})


# =========================
# LANÇAR
# =========================
@app.route("/lancar", methods=["POST"])
def lancar():
    guard = require_login()
    if guard:
        return jsonify(guard[0]), guard[1]

    user_email = session["user_email"]
    dataj = request.get_json(silent=True) or {}

    tipo = str(dataj.get("tipo", "")).strip()
    categoria = str(dataj.get("categoria", "")).strip()
    descricao = str(dataj.get("descricao", "")).strip()
    valor = money_to_float(dataj.get("valor"))
    data_br = str(dataj.get("data", "")).strip()  # dd/mm/aaaa or ""

    if not tipo or not categoria or not descricao:
        return jsonify({"ok": False, "msg": "Campos obrigatórios: tipo, categoria, descricao"}), 400
    if valor <= 0:
        return jsonify({"ok": False, "msg": "Valor inválido"}), 400

    if data_br:
        d = parse_date_br(data_br)
        if not d:
            return jsonify({"ok": False, "msg": "Data inválida. Use dd/mm/aaaa"}), 400
    else:
        data_br = date.today().strftime("%d/%m/%Y")

    ws = lanc_ws()
    ws.append_row(
        [user_email, tipo, categoria, descricao, f"{valor:.2f}", data_br, now_str()],
        value_input_option="RAW"
    )
    return jsonify({"ok": True})


# =========================
# FILTERS
# =========================
def apply_filters(items: List[Dict[str, Any]], params: Dict[str, Any]) -> List[Dict[str, Any]]:
    month = params.get("month")
    year = params.get("year")
    tipo = params.get("tipo", "Todos")
    q = params.get("q", "")
    date_from = params.get("date_from", "")
    date_to = params.get("date_to", "")
    vmin = params.get("value_min", "")
    vmax = params.get("value_max", "")

    df = None
    dt = None
    if date_from:
        try:
            df = datetime.strptime(date_from, "%Y-%m-%d").date()
        except Exception:
            df = None
    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d").date()
        except Exception:
            dt = None

    vmin_n = money_to_float(vmin) if vmin else None
    vmax_n = money_to_float(vmax) if vmax else None

    out = []
    for it in items:
        if tipo and tipo != "Todos":
            if str(it.get("Tipo", "")).strip() != tipo:
                continue

        if q and not match_query(it, q):
            continue

        d = parse_date_br(str(it.get("Data", "")))
        if not d:
            continue

        if month and year:
            if not (d.month == int(month) and d.year == int(year)):
                continue

        if df and d < df:
            continue
        if dt and d > dt:
            continue

        val = item_value_num(it)
        if vmin_n is not None and val < vmin_n:
            continue
        if vmax_n is not None and val > vmax_n:
            continue

        out.append(it)

    return out


@app.route("/ultimos")
def ultimos():
    guard = require_login()
    if guard:
        return jsonify(guard[0]), guard[1]

    user_email = session["user_email"]
    ws = lanc_ws()
    items = get_records(ws)
    items = [i for i in items if safe_lower(i.get("Email")) == safe_lower(user_email)]

    month = request.args.get("month", type=int)
    year = request.args.get("year", type=int)
    tipo = request.args.get("tipo", default="Todos", type=str)
    q = request.args.get("q", default="", type=str)
    order = request.args.get("order", default="recent", type=str)
    date_from = request.args.get("date_from", default="", type=str)
    date_to = request.args.get("date_to", default="", type=str)
    value_min = request.args.get("value_min", default="", type=str)
    value_max = request.args.get("value_max", default="", type=str)
    page = request.args.get("page", default=1, type=int)
    limit = request.args.get("limit", default=10, type=int)

    filtered = apply_filters(items, {
        "month": month, "year": year, "tipo": tipo, "q": q,
        "date_from": date_from, "date_to": date_to,
        "value_min": value_min, "value_max": value_max
    })

    if order == "oldest":
        filtered.sort(key=lambda x: (item_date_num(x), x.get("_row", 0)))
    elif order == "value_desc":
        filtered.sort(key=lambda x: item_value_num(x), reverse=True)
    elif order == "value_asc":
        filtered.sort(key=lambda x: item_value_num(x))
    else:
        filtered.sort(key=lambda x: (item_date_num(x), x.get("_row", 0)), reverse=True)

    total = len(filtered)
    limit = max(1, min(int(limit or 10), 200))
    page = max(1, int(page or 1))
    start = (page - 1) * limit
    end = start + limit
    page_items = filtered[start:end]

    return jsonify({"ok": True, "total": total, "items": page_items})


def compute_resumo(user_email: str, month: Optional[int], year: Optional[int], tipo: str, q: str,
                  date_from: str, date_to: str, value_min: str, value_max: str) -> Dict[str, Any]:
    ws = lanc_ws()
    items = get_records(ws)
    items = [i for i in items if safe_lower(i.get("Email")) == safe_lower(user_email)]

    filtered = apply_filters(items, {
        "month": month, "year": year, "tipo": tipo, "q": q,
        "date_from": date_from, "date_to": date_to,
        "value_min": value_min, "value_max": value_max
    })

    entradas = 0.0
    saidas = 0.0

    dias_labels: List[str] = []
    serie_receita: List[float] = []
    serie_gasto: List[float] = []

    if month and year:
        import calendar
        last_day = calendar.monthrange(year, month)[1]
        dias_labels = [str(d).zfill(2) for d in range(1, last_day + 1)]
        serie_receita = [0.0] * last_day
        serie_gasto = [0.0] * last_day

    gastos_cat: Dict[str, float] = {}
    receitas_cat: Dict[str, float] = {}

    for it in filtered:
        t = str(it.get("Tipo", "")).strip()
        cat = str(it.get("Categoria", "")).strip() or "Sem categoria"
        val = item_value_num(it)
        d = parse_date_br(str(it.get("Data", "")))

        if "Rece" in t:
            entradas += val
            receitas_cat[cat] = receitas_cat.get(cat, 0.0) + val
            if month and year and d and d.month == month and d.year == year:
                serie_receita[d.day - 1] += val
        else:
            saidas += val
            gastos_cat[cat] = gastos_cat.get(cat, 0.0) + val
            if month and year and d and d.month == month and d.year == year:
                serie_gasto[d.day - 1] += val

    saldo = entradas - saidas

    def topcats(dct: Dict[str, float]) -> List[Dict[str, Any]]:
        arr = [{"categoria": k, "total": v} for k, v in dct.items()]
        arr.sort(key=lambda x: x["total"], reverse=True)
        return arr

    gastos_arr = topcats(gastos_cat)
    receitas_arr = topcats(receitas_cat)

    return {
        "ok": True,
        "entradas": entradas,
        "saidas": saidas,
        "saldo": saldo,

        "dias": dias_labels,
        "serie_receita": serie_receita,
        "serie_gasto": serie_gasto,

        "pizza_gastos_labels": [x["categoria"] for x in gastos_arr[:12]],
        "pizza_gastos_values": [x["total"] for x in gastos_arr[:12]],
        "pizza_receitas_labels": [x["categoria"] for x in receitas_arr[:12]],
        "pizza_receitas_values": [x["total"] for x in receitas_arr[:12]],

        "gastos_categorias": gastos_arr,
        "receitas_categorias": receitas_arr,
    }


@app.route("/resumo")
def resumo():
    guard = require_login()
    if guard:
        return jsonify(guard[0]), guard[1]

    user_email = session["user_email"]

    month = request.args.get("month", type=int)
    year = request.args.get("year", type=int)
    tipo = request.args.get("tipo", default="Todos", type=str)
    q = request.args.get("q", default="", type=str)
    date_from = request.args.get("date_from", default="", type=str)
    date_to = request.args.get("date_to", default="", type=str)
    value_min = request.args.get("value_min", default="", type=str)
    value_max = request.args.get("value_max", default="", type=str)

    payload = compute_resumo(user_email, month, year, tipo, q, date_from, date_to, value_min, value_max)
    return jsonify(payload)


@app.route("/dashboard")
def dashboard():
    """
    Dashboard completo: resumo + metas + progresso + insights + alertas.
    """
    guard = require_login()
    if guard:
        return jsonify(guard[0]), guard[1]

    user_email = session["user_email"]

    month = request.args.get("month", type=int)
    year = request.args.get("year", type=int)
    tipo = request.args.get("tipo", default="Todos", type=str)
    q = request.args.get("q", default="", type=str)
    date_from = request.args.get("date_from", default="", type=str)
    date_to = request.args.get("date_to", default="", type=str)
    value_min = request.args.get("value_min", default="", type=str)
    value_max = request.args.get("value_max", default="", type=str)

    if not month or not year:
        month, year = current_month_year()

    base = compute_resumo(user_email, month, year, tipo, q, date_from, date_to, value_min, value_max)
    meta = _get_meta(user_email, int(month), int(year))

    entradas = float(base.get("entradas") or 0.0)
    saidas = float(base.get("saidas") or 0.0)
    saldo = float(base.get("saldo") or 0.0)

    mr = float(meta.get("meta_receitas") or 0.0)
    mg = float(meta.get("meta_gastos") or 0.0)

    def pct(val: float, target: float) -> float:
        if target <= 0:
            return 0.0
        return round((val / target) * 100.0, 2)

    progresso = {
        "receitas_pct": pct(entradas, mr),
        "gastos_pct": pct(saidas, mg),
        "receitas_restante": max(0.0, mr - entradas),
        "gastos_restante": max(0.0, mg - saidas),
    }

    # simple insights (server-side)
    top_gasto = base.get("gastos_categorias", [])[:1]
    top_receita = base.get("receitas_categorias", [])[:1]

    insights = []
    if top_gasto:
        insights.append({
            "title": "Maior gasto do mês",
            "desc": f"{top_gasto[0]['categoria']}: R$ {top_gasto[0]['total']:.2f}"
        })
    if top_receita:
        insights.append({
            "title": "Maior receita do mês",
            "desc": f"{top_receita[0]['categoria']}: R$ {top_receita[0]['total']:.2f}"
        })
    if saldo < 0:
        insights.append({
            "title": "Atenção ao saldo",
            "desc": "Seu saldo está negativo neste período. Revise categorias e metas."
        })

    # alerts (server-side simple)
    alerts = []
    if mg > 0 and saidas > mg:
        alerts.append({
            "level": "danger",
            "title": "Meta de gastos estourada",
            "desc": f"Você gastou R$ {saidas:.2f} e a meta era R$ {mg:.2f}."
        })
    elif mg > 0 and progresso["gastos_pct"] >= 85:
        alerts.append({
            "level": "warn",
            "title": "Você está perto do limite de gastos",
            "desc": f"Você já usou {progresso['gastos_pct']:.0f}% do limite."
        })

    if mr > 0 and entradas >= mr:
        alerts.append({
            "level": "ok",
            "title": "Meta de receitas batida",
            "desc": f"Você fez R$ {entradas:.2f} e a meta era R$ {mr:.2f}."
        })

    if saldo < 0:
        alerts.append({
            "level": "warn",
            "title": "Saldo negativo no período",
            "desc": f"Saldo: R$ {saldo:.2f}. Ajuste metas e categorias."
        })

    return jsonify({
        **base,
        "metas": meta,
        "progresso": progresso,
        "insights": insights,
        "alerts": alerts
    })


# =========================
# EDITAR / EXCLUIR
# =========================
@app.route("/lancamento/<int:row>", methods=["PATCH"])
def editar_lancamento(row: int):
    guard = require_login()
    if guard:
        return jsonify(guard[0]), guard[1]

    user_email = session["user_email"]
    if row < 2:
        return jsonify({"ok": False, "msg": "Linha inválida"}), 400

    dataj = request.get_json(silent=True) or {}
    tipo = str(dataj.get("tipo", "")).strip()
    categoria = str(dataj.get("categoria", "")).strip()
    descricao = str(dataj.get("descricao", "")).strip()
    valor = money_to_float(dataj.get("valor"))
    data_br = str(dataj.get("data", "")).strip()

    if not tipo or not categoria or not descricao or not data_br:
        return jsonify({"ok": False, "msg": "Campos obrigatórios"}), 400
    if valor <= 0:
        return jsonify({"ok": False, "msg": "Valor inválido"}), 400
    if not parse_date_br(data_br):
        return jsonify({"ok": False, "msg": "Data inválida. Use dd/mm/aaaa"}), 400

    ws = lanc_ws()
    row_vals = ws.row_values(row)
    headers = ws.row_values(1)
    if not row_vals:
        return jsonify({"ok": False, "msg": "Lançamento não encontrado"}), 404

    col = {h: idx + 1 for idx, h in enumerate(headers)}
    email_in_row = (row_vals[col["Email"] - 1] if "Email" in col and len(row_vals) >= col["Email"] else "").strip().lower()
    if email_in_row != user_email.lower():
        return jsonify({"ok": False, "msg": "Sem permissão"}), 403

    ws.update_cell(row, col["Tipo"], tipo)
    ws.update_cell(row, col["Categoria"], categoria)
    ws.update_cell(row, col["Descrição"], descricao)
    ws.update_cell(row, col["Valor"], f"{valor:.2f}")
    ws.update_cell(row, col["Data"], data_br)

    return jsonify({"ok": True})


@app.route("/lancamento/<int:row>", methods=["DELETE"])
def excluir_lancamento(row: int):
    guard = require_login()
    if guard:
        return jsonify(guard[0]), guard[1]

    user_email = session["user_email"]
    if row < 2:
        return jsonify({"ok": False, "msg": "Linha inválida"}), 400

    ws = lanc_ws()
    headers = ws.row_values(1)
    col = {h: idx + 1 for idx, h in enumerate(headers)}

    row_vals = ws.row_values(row)
    if not row_vals:
        return jsonify({"ok": False, "msg": "Lançamento não encontrado"}), 404

    email_in_row = (row_vals[col["Email"] - 1] if "Email" in col and len(row_vals) >= col["Email"] else "").strip().lower()
    if email_in_row != user_email.lower():
        return jsonify({"ok": False, "msg": "Sem permissão"}), 403

    ws.delete_rows(row)
    return jsonify({"ok": True})


# =========================
# EXPORT CSV
# =========================
@app.route("/export.csv")
def export_csv():
    guard = require_login()
    if guard:
        return jsonify(guard[0]), guard[1]

    user_email = session["user_email"]
    ws = lanc_ws()
    items = get_records(ws)
    items = [i for i in items if safe_lower(i.get("Email")) == safe_lower(user_email)]

    month = request.args.get("month", type=int)
    year = request.args.get("year", type=int)
    tipo = request.args.get("tipo", default="Todos", type=str)
    q = request.args.get("q", default="", type=str)
    order = request.args.get("order", default="recent", type=str)
    date_from = request.args.get("date_from", default="", type=str)
    date_to = request.args.get("date_to", default="", type=str)
    value_min = request.args.get("value_min", default="", type=str)
    value_max = request.args.get("value_max", default="", type=str)

    filtered = apply_filters(items, {
        "month": month, "year": year, "tipo": tipo, "q": q,
        "date_from": date_from, "date_to": date_to,
        "value_min": value_min, "value_max": value_max
    })

    if order == "oldest":
        filtered.sort(key=lambda x: (item_date_num(x), x.get("_row", 0)))
    elif order == "value_desc":
        filtered.sort(key=lambda x: item_value_num(x), reverse=True)
    elif order == "value_asc":
        filtered.sort(key=lambda x: item_value_num(x))
    else:
        filtered.sort(key=lambda x: (item_date_num(x), x.get("_row", 0)), reverse=True)

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Tipo", "Categoria", "Descrição", "Valor", "Data"])
    for it in filtered:
        w.writerow([it.get("Tipo", ""), it.get("Categoria", ""), it.get("Descrição", ""), it.get("Valor", ""), it.get("Data", "")])

    resp = make_response(out.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=finance-ai.csv"
    return resp


@app.route("/export.pdf")
def export_pdf():
    return jsonify({"ok": False, "msg": "PDF não habilitado nesta versão estável"}), 400


if __name__ == "__main__":
    # Local dev tip: COOKIE_SECURE=0
    app.run(host="0.0.0.0", port=5000, debug=True)
