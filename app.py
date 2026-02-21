import os
import json
import uuid
import csv
import io
from datetime import datetime, date
from calendar import monthrange
from typing import Any, Dict, List, Tuple, Optional

from flask import Flask, jsonify, request, render_template, Response, abort, session
import gspread
from google.oauth2.service_account import Credentials

from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

# PDF (reportlab)
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as pdf_canvas

app = Flask(__name__)

# Render/Proxy: garante que Flask "enxerga" https corretamente
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Sessão (cookie)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,   # Render = https
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ====== HEADERS ======
# Agora com UserEmail no final
HEADERS_LANC = ["ID", "Data", "Tipo", "Categoria", "Descrição", "Valor", "CreatedAt", "UserEmail"]
HEADERS_USERS = ["Email", "PasswordHash", "Ativo", "CreatedAt"]

_client_cached: Optional[gspread.Client] = None


# =========================
# AUTH / SESSÃO
# =========================
def is_logged() -> bool:
    return bool(session.get("user_email"))

def current_user_email() -> str:
    return (session.get("user_email") or "").strip().lower()

def _legacy_admin_enabled() -> bool:
    # fallback opcional (se quiser manter)
    email = os.getenv("APP_EMAIL", "").strip()
    pwd_hash = os.getenv("APP_PASSWORD_HASH", "").strip()
    return bool(email and pwd_hash)

def require_auth():
    if not is_logged():
        abort(401)

def require_admin():
    """
    Admin por token (header) para criar usuários.
    """
    token = (request.headers.get("X-ADMIN-TOKEN", "") or "").strip()
    expected = (os.getenv("ADMIN_TOKEN", "") or "").strip()
    if not expected or token != expected:
        abort(403)


@app.before_request
def _auth_middleware():
    public_paths = {"/", "/login", "/me"}
    if request.path in public_paths:
        return
    if request.path.startswith("/static"):
        return
    # rotas admin exigem login + token
    if request.path.startswith("/admin/"):
        require_auth()
        require_admin()
        return
    # demais exigem login
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


def _open_sheet() -> gspread.Spreadsheet:
    sheet_id = os.getenv("SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("Missing env var SHEET_ID")
    client = get_client()
    return client.open_by_key(sheet_id)


def _get_or_create_ws(sh: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    try:
        return sh.worksheet(title)
    except:
        return sh.add_worksheet(title=title, rows=2000, cols=20)


def ensure_headers_exact(ws: gspread.Worksheet, headers: List[str]):
    values = ws.get_all_values()
    if not values:
        ws.append_row(headers)
        return
    first = [c.strip() for c in (values[0] or [])]
    if first != headers:
        ws.update("A1", [headers])


def ensure_headers_lanc(ws: gspread.Worksheet):
    """
    Migração automática:
    - se planilha estiver vazia -> cria HEADERS_LANC
    - se estiver com header antigo (sem UserEmail) -> adiciona coluna no header
      e completa vazio nas linhas antigas.
    """
    values = ws.get_all_values()
    if not values:
        ws.append_row(HEADERS_LANC)
        return

    first = [c.strip() for c in (values[0] or [])]

    # Caso já esteja correto
    if first == HEADERS_LANC:
        return

    # Caso seja header antigo sem UserEmail
    if first == ["ID", "Data", "Tipo", "Categoria", "Descrição", "Valor", "CreatedAt"]:
        # Atualiza header
        ws.update("A1", [HEADERS_LANC])

        # Preenche coluna UserEmail vazia para linhas existentes (opcional)
        # Não tentamos reescrever tudo, apenas garantimos que a coluna exista.
        return

    # Outro caso: força header exato (pode sobrescrever headers estranhos)
    ws.update("A1", [HEADERS_LANC])


def get_lanc_ws() -> gspread.Worksheet:
    sh = _open_sheet()
    ws_name = os.getenv("SHEET_TAB", "").strip() or "Lancamentos"
    ws = _get_or_create_ws(sh, ws_name)
    ensure_headers_lanc(ws)
    return ws


def get_users_ws() -> gspread.Worksheet:
    sh = _open_sheet()
    users_tab = os.getenv("USERS_TAB", "").strip() or "Usuarios"
    ws = _get_or_create_ws(sh, users_tab)
    ensure_headers_exact(ws, HEADERS_USERS)
    return ws


# =========================
# PARSERS
# =========================
def parse_br_date(s: str) -> date:
    return datetime.strptime(s.strip(), "%d/%m/%Y").date()

def parse_iso_date(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()

def parse_any_date(s: str) -> Optional[date]:
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        if "-" in s:
            return parse_iso_date(s)
        return parse_br_date(s)
    except:
        return None

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
    except:
        return 0.0


# =========================
# USERS: BUSCA/VALIDAÇÃO
# =========================
def find_user(email: str) -> Optional[Dict[str, str]]:
    email = (email or "").strip().lower()
    if not email:
        return None

    ws = get_users_ws()
    values = ws.get_all_values()
    if len(values) < 2:
        return None

    headers = values[0]
    rows = values[1:]

    idx_email = headers.index("Email")
    idx_hash = headers.index("PasswordHash")
    idx_ativo = headers.index("Ativo")

    for r in rows:
        e = (r[idx_email] if idx_email < len(r) else "").strip().lower()
        if e == email:
            h = (r[idx_hash] if idx_hash < len(r) else "").strip()
            ativo = (r[idx_ativo] if idx_ativo < len(r) else "1").strip()
            return {"email": e, "hash": h, "ativo": ativo}
    return None


def validate_login(email: str, password: str) -> bool:
    email = (email or "").strip().lower()
    password = (password or "").strip()

    # 1) tenta validar pelo Usuarios
    u = find_user(email)
    if u and u.get("ativo") == "1" and u.get("hash"):
        return check_password_hash(u["hash"], password)

    # 2) fallback admin legacy (opcional)
    if _legacy_admin_enabled():
        app_email = os.getenv("APP_EMAIL", "").strip().lower()
        pwd_hash = os.getenv("APP_PASSWORD_HASH", "").strip()
        if email == app_email and check_password_hash(pwd_hash, password):
            return True

    return False


# =========================
# ROWS
# =========================
def get_rows_with_rownum(ws: gspread.Worksheet) -> Tuple[List[str], List[Dict[str, Any]]]:
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return HEADERS_LANC, []

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


# =========================
# FILTERS
# =========================
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
    t = (request.args.get("tipo", default="Todos", type=str) or "Todos").strip()
    if t not in ("Todos", "Gasto", "Receita"):
        t = "Todos"
    return t

def get_order() -> str:
    o = (request.args.get("order", default="recent", type=str) or "recent").strip()
    if o not in ("recent", "oldest", "value_desc", "value_asc"):
        o = "recent"
    return o

def get_date_range() -> Tuple[Optional[date], Optional[date]]:
    dfrom = parse_any_date(request.args.get("date_from", default="", type=str))
    dto = parse_any_date(request.args.get("date_to", default="", type=str))
    return dfrom, dto

def get_value_range() -> Tuple[Optional[float], Optional[float]]:
    vmin_raw = request.args.get("value_min", default="", type=str)
    vmax_raw = request.args.get("value_max", default="", type=str)

    vmin = safe_float(vmin_raw) if str(vmin_raw).strip() else None
    vmax = safe_float(vmax_raw) if str(vmax_raw).strip() else None

    if vmin is not None and vmin < 0:
        vmin = None
    if vmax is not None and vmax < 0:
        vmax = None

    if vmin is not None and vmax is not None and vmin > vmax:
        vmin, vmax = vmax, vmin

    return vmin, vmax


def filter_rows(
    rows: List[Dict[str, Any]],
    month: int,
    year: int,
    q: str,
    tipo_filter: str,
    dfrom: Optional[date],
    dto: Optional[date],
    vmin: Optional[float],
    vmax: Optional[float],
    user_email: str,
) -> List[Dict[str, Any]]:
    q = (q or "").strip().lower()
    user_email = (user_email or "").strip().lower()

    filtered: List[Dict[str, Any]] = []
    for r in rows:
        # >>> multi-usuário: só do dono
        owner = (r.get("UserEmail") or "").strip().lower()
        if owner and user_email and owner != user_email:
            continue
        # se owner estiver vazio (dados antigos), você pode:
        # - ignorar (não mostrar)
        # - OU mostrar somente para admin legacy
        # Aqui: se vazio, não mostra (segurança)
        if not owner:
            continue

        d_str = (r.get("Data") or "").strip()
        d = parse_any_date(d_str)
        if not d:
            continue

        if dfrom or dto:
            if dfrom and d < dfrom:
                continue
            if dto and d > dto:
                continue
        else:
            if d.month != month or d.year != year:
                continue

        tipo = (r.get("Tipo") or "").strip()
        if tipo_filter != "Todos" and tipo != tipo_filter:
            continue

        val = safe_float(r.get("Valor"))
        if vmin is not None and val < vmin:
            continue
        if vmax is not None and val > vmax:
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


def sort_rows(rows: List[Dict[str, Any]], order: str) -> List[Dict[str, Any]]:
    def dkey(r: Dict[str, Any]) -> date:
        d = parse_any_date(r.get("Data", ""))
        return d or date(1900, 1, 1)

    def vkey(r: Dict[str, Any]) -> float:
        return safe_float(r.get("Valor"))

    def rownum(r: Dict[str, Any]) -> int:
        try:
            return int(r.get("_row", 0) or 0)
        except:
            return 0

    if order == "oldest":
        return sorted(rows, key=lambda r: (dkey(r), rownum(r)))
    if order == "value_asc":
        return sorted(rows, key=lambda r: (vkey(r), dkey(r), rownum(r)))
    if order == "value_desc":
        return sorted(rows, key=lambda r: (-vkey(r), dkey(r), rownum(r)))
    return sorted(rows, key=lambda r: (dkey(r), rownum(r)), reverse=True)


# =========================
# ROUTES
# =========================
@app.get("/")
def home():
    return render_template("index.html")


@app.get("/me")
def me():
    if not is_logged():
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "email": current_user_email()})


@app.post("/login")
def login():
    body = request.get_json(force=True, silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", "")).strip()

    if not email or not password:
        return jsonify({"ok": False, "msg": "Informe e-mail e senha"}), 400

    if not validate_login(email, password):
        return jsonify({"ok": False, "msg": "Credenciais inválidas"}), 401

    session["user_email"] = email
    return jsonify({"ok": True})


@app.post("/logout")
def logout():
    session.pop("user_email", None)
    return jsonify({"ok": True})


# ===== ADMIN: criar usuário =====
@app.post("/admin/create_user")
def admin_create_user():
    """
    Cria usuário na aba Usuarios.
    Protegido por:
    - estar logado
    - header X-ADMIN-TOKEN == ADMIN_TOKEN
    """
    body = request.get_json(force=True, silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", "")).strip()

    if not email or not password:
        return jsonify({"ok": False, "msg": "Informe email e password"}), 400

    if find_user(email):
        return jsonify({"ok": False, "msg": "Usuário já existe"}), 409

    ws = get_users_ws()
    pwd_hash = generate_password_hash(password)  # pbkdf2:sha256...
    created_at = datetime.utcnow().isoformat()

    ws.append_row([email, pwd_hash, "1", created_at])
    return jsonify({"ok": True, "msg": "Usuário criado"})


@app.post("/lancar")
def lancar():
    ws = get_lanc_ws()
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
    if not parse_any_date(data_str):
        return jsonify({"ok": False, "msg": "Data inválida. Use dd/mm/aaaa."}), 400

    v = safe_float(valor)
    if v <= 0:
        return jsonify({"ok": False, "msg": "Valor inválido."}), 400

    new_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()
    owner = current_user_email()

    # salva como número no Sheets + dono
    ws.append_row([new_id, data_str, tipo, categoria, descricao, v, created_at, owner])

    return jsonify({"ok": True, "msg": "Lançamento salvo!"})


@app.get("/ultimos")
def ultimos():
    ws = get_lanc_ws()
    _, rows = get_rows_with_rownum(ws)

    month, year = get_month_year_from_request()
    q = request.args.get("q", default="", type=str)
    tipo_filter = get_tipo_filter()
    order = get_order()
    dfrom, dto = get_date_range()
    vmin, vmax = get_value_range()

    limit = request.args.get("limit", default=10, type=int)
    page = request.args.get("page", default=1, type=int)
    if limit < 1: limit = 10
    if limit > 200: limit = 200
    if page < 1: page = 1

    filtered = filter_rows(rows, month, year, q, tipo_filter, dfrom, dto, vmin, vmax, current_user_email())
    ordered = sort_rows(filtered, order)

    total = len(ordered)
    start = (page - 1) * limit
    end = start + limit
    items = ordered[start:end]

    return jsonify({
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "order": order
    })


@app.get("/resumo")
def resumo():
    ws = get_lanc_ws()
    _, rows = get_rows_with_rownum(ws)

    month, year = get_month_year_from_request()
    q = request.args.get("q", default="", type=str)
    tipo_filter = get_tipo_filter()
    dfrom, dto = get_date_range()
    vmin, vmax = get_value_range()

    filtered = filter_rows(rows, month, year, q, tipo_filter, dfrom, dto, vmin, vmax, current_user_email())

    last_day = monthrange(year, month)[1]
    dias = [str(i + 1).zfill(2) for i in range(last_day)]

    entradas = 0.0
    saidas = 0.0
    serie_receita = [0.0] * last_day
    serie_gasto = [0.0] * last_day

    pizza_gastos: Dict[str, float] = {}
    pizza_receitas: Dict[str, float] = {}

    for r in filtered:
        tipo = (r.get("Tipo") or "").strip()
        cat = (r.get("Categoria") or "Sem categoria").strip() or "Sem categoria"
        val = safe_float(r.get("Valor"))

        d = parse_any_date(r.get("Data", ""))
        if not d:
            continue

        if d.month == month and d.year == year:
            di = d.day - 1
            if 0 <= di < last_day:
                if tipo == "Receita":
                    serie_receita[di] += val
                else:
                    serie_gasto[di] += val

        if tipo == "Receita":
            entradas += val
            pizza_receitas[cat] = pizza_receitas.get(cat, 0.0) + val
        else:
            saidas += val
            pizza_gastos[cat] = pizza_gastos.get(cat, 0.0) + val

    saldo = entradas - saidas

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

    gastos_table = [{"categoria": k, "total": round(v, 2)} for k, v in sorted(pizza_gastos.items(), key=lambda x: x[1], reverse=True)]
    receitas_table = [{"categoria": k, "total": round(v, 2)} for k, v in sorted(pizza_receitas.items(), key=lambda x: x[1], reverse=True)]

    return jsonify({
        "month": month,
        "year": year,
        "tipo": tipo_filter,
        "date_from": request.args.get("date_from", ""),
        "date_to": request.args.get("date_to", ""),
        "value_min": request.args.get("value_min", ""),
        "value_max": request.args.get("value_max", ""),
        "entradas": round(entradas, 2),
        "saidas": round(saidas, 2),
        "saldo": round(saldo, 2),
        "dias": dias,
        "serie_receita": [round(x, 2) for x in serie_receita],
        "serie_gasto": [round(x, 2) for x in serie_gasto],
        "pizza_gastos_labels": pg_l,
        "pizza_gastos_values": pg_v,
        "pizza_receitas_labels": pr_l,
        "pizza_receitas_values": pr_v,
        "gastos_categorias": gastos_table,
        "receitas_categorias": receitas_table
    })


@app.patch("/lancamento/<int:row>")
def editar(row: int):
    ws = get_lanc_ws()
    body = request.get_json(force=True, silent=True) or {}

    if row <= 1:
        return jsonify({"ok": False, "msg": "Linha inválida."}), 400

    # pega linha atual pra validar dono
    values = ws.row_values(row)
    headers = ws.row_values(1)
    try:
        idx_owner = headers.index("UserEmail")
        owner = (values[idx_owner] if idx_owner < len(values) else "").strip().lower()
    except:
        owner = ""

    if not owner or owner != current_user_email():
        return jsonify({"ok": False, "msg": "Sem permissão."}), 403

    tipo = str(body.get("tipo", "")).strip()
    categoria = str(body.get("categoria", "")).strip()
    descricao = str(body.get("descricao", "")).strip()
    valor = body.get("valor", None)
    data_str = str(body.get("data", "")).strip()

    if tipo not in ("Gasto", "Receita"):
        return jsonify({"ok": False, "msg": "Tipo inválido (Gasto ou Receita)."}), 400
    if not categoria or not descricao or not data_str:
        return jsonify({"ok": False, "msg": "Preencha categoria, descrição e data."}), 400
    if not parse_any_date(data_str):
        return jsonify({"ok": False, "msg": "Data inválida. Use dd/mm/aaaa."}), 400

    v = safe_float(valor)
    if v <= 0:
        return jsonify({"ok": False, "msg": "Valor inválido."}), 400

    ws.update(f"B{row}", [[data_str]])
    ws.update(f"C{row}", [[tipo]])
    ws.update(f"D{row}", [[categoria]])
    ws.update(f"E{row}", [[descricao]])
    ws.update(f"F{row}", [[v]])  # número

    return jsonify({"ok": True, "msg": "Editado com sucesso!"})


@app.delete("/lancamento/<int:row>")
def deletar(row: int):
    ws = get_lanc_ws()
    if row <= 1:
        return jsonify({"ok": False, "msg": "Linha inválida."}), 400

    values = ws.row_values(row)
    headers = ws.row_values(1)
    try:
        idx_owner = headers.index("UserEmail")
        owner = (values[idx_owner] if idx_owner < len(values) else "").strip().lower()
    except:
        owner = ""

    if not owner or owner != current_user_email():
        return jsonify({"ok": False, "msg": "Sem permissão."}), 403

    ws.delete_rows(row)
    return jsonify({"ok": True, "msg": "Excluído com sucesso!"})


def build_filtered_for_export() -> List[Dict[str, Any]]:
    ws = get_lanc_ws()
    _, rows = get_rows_with_rownum(ws)

    month, year = get_month_year_from_request()
    q = request.args.get("q", default="", type=str)
    tipo_filter = get_tipo_filter()
    order = get_order()
    dfrom, dto = get_date_range()
    vmin, vmax = get_value_range()

    filtered = filter_rows(rows, month, year, q, tipo_filter, dfrom, dto, vmin, vmax, current_user_email())
    ordered = sort_rows(filtered, order)
    return ordered


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
    order = get_order()
    dfrom, dto = get_date_range()
    vmin, vmax = get_value_range()

    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    y = h - 40
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, f"Finance AI — Relatório {str(month).zfill(2)}/{year} — {tipo_filter}")
    y -= 18

    c.setFont("Helvetica", 10)
    if dfrom or dto:
        c.drawString(40, y, f"Intervalo: {request.args.get('date_from','')} até {request.args.get('date_to','')}")
        y -= 16
    if vmin is not None or vmax is not None:
        c.drawString(40, y, f"Valor: min {request.args.get('value_min','')} / max {request.args.get('value_max','')}")
        y -= 16
    if q:
        c.drawString(40, y, f"Busca: {q}")
        y -= 16
    c.drawString(40, y, f"Ordenação: {order}")
    y -= 18

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
