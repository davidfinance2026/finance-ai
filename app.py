import os
import io
import csv
import json
import math
import re
from datetime import datetime, date
from typing import Dict, Any, List, Tuple, Optional

from flask import Flask, request, jsonify, session, send_file
from werkzeug.security import generate_password_hash, check_password_hash

import gspread
from google.oauth2.service_account import Credentials

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


# =========================
# CONFIG
# =========================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

APP_EMAIL = os.getenv("APP_EMAIL", "admin@financeai.com").strip().lower()
# APP_PASSWORD_HASH deve ser um hash pbkdf2:sha256...
APP_PASSWORD_HASH = os.getenv("APP_PASSWORD_HASH", "").strip()

FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev_secret_key_change_me")

# Planilha "MASTER" apenas para registrar usuários (aba Usuarios)
MASTER_SHEET_ID = os.getenv("MASTER_SHEET_ID", "").strip()
USERS_TAB = os.getenv("USERS_TAB", "Usuarios").strip()

# Se você quiser copiar um template (recomendado), informe o ID de um Google Sheet modelo
# com a aba "Lancamentos" e cabeçalho pronto.
TEMPLATE_SHEET_ID = os.getenv("TEMPLATE_SHEET_ID", "").strip()

# Se quiser que as planilhas novas caiam dentro de uma pasta do Drive, informe o folder id
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "").strip()

DEFAULT_USER_TAB = os.getenv("DEFAULT_USER_TAB", "Lancamentos").strip()

# =========================
# FLASK
# =========================
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = FLASK_SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Se estiver usando HTTPS (Render), deixe True.
# Se estiver testando local em http, pode dar problema. Ajuste se necessário.
app.config["SESSION_COOKIE_SECURE"] = True


# =========================
# GOOGLE CLIENT (gspread)
# =========================
_client_cached: Optional[gspread.Client] = None

def get_client() -> gspread.Client:
    """
    Prioridade:
    1) SERVICE_ACCOUNT_JSON (env com JSON inteiro)
    2) Secret File do Render em /etc/secrets/google_creds.json
    3) arquivo local google_creds.json
    """
    global _client_cached
    if _client_cached is not None:
        return _client_cached

    creds_json_env = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()
    if creds_json_env:
        info = json.loads(creds_json_env)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    elif os.path.exists("/etc/secrets/google_creds.json"):
        creds = Credentials.from_service_account_file("/etc/secrets/google_creds.json", scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("google_creds.json", scopes=SCOPES)

    _client_cached = gspread.authorize(creds)
    return _client_cached


# =========================
# HELPERS (auth)
# =========================
def is_logged() -> bool:
    return bool(session.get("user_email"))

def require_login():
    if not is_logged():
        return jsonify({"ok": False, "msg": "Não autenticado"}), 401
    return None

def is_admin() -> bool:
    email = (session.get("user_email") or "").strip().lower()
    return email == APP_EMAIL

def require_admin():
    if not is_logged():
        return jsonify({"ok": False, "msg": "Não autenticado"}), 401
    if not is_admin():
        return jsonify({"ok": False, "msg": "Acesso negado"}), 403
    return None


# =========================
# HELPERS (money/date)
# =========================
def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def parse_money_br(value) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    s = s.replace("R$", "").strip()

    has_comma = "," in s
    has_dot = "." in s

    if has_comma and has_dot:
        # último separador define decimal
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "")
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        s = s.replace(".", "")
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")

    try:
        n = float(s)
        if math.isfinite(n):
            return n
        return None
    except:
        return None

def br_date_to_iso(dmy: str) -> Optional[str]:
    # "21/02/2026" -> "2026-02-21"
    if not dmy:
        return None
    m = re.match(r"^\s*(\d{2})/(\d{2})/(\d{4})\s*$", str(dmy))
    if not m:
        return None
    dd, mm, yy = m.group(1), m.group(2), m.group(3)
    return f"{yy}-{mm}-{dd}"

def iso_to_br(d: str) -> str:
    # "2026-02-21" -> "21/02/2026"
    if not d:
        return ""
    parts = str(d).split("-")
    if len(parts) == 3:
        y, m, dd = parts
        return f"{dd}/{m}/{y}"
    return str(d)

def month_year_from_iso(iso: str) -> Tuple[int, int]:
    # "2026-02-21" -> (2, 2026)
    y, m, _ = iso.split("-")
    return int(m), int(y)


# =========================
# MASTER USERS SHEET
# =========================
def open_users_ws():
    if not MASTER_SHEET_ID:
        raise RuntimeError("Env MASTER_SHEET_ID não definido.")
    gc = get_client()
    sh = gc.open_by_key(MASTER_SHEET_ID)
    ws = sh.worksheet(USERS_TAB)
    return ws

def ensure_users_header(ws):
    # Email | PasswordHash | Ativo | CreatedAt | SheetId | SheetTab
    header = ws.row_values(1)
    wanted = ["Email", "PasswordHash", "Ativo", "CreatedAt", "SheetId", "SheetTab"]
    if [h.strip() for h in header] != wanted:
        ws.update("A1:F1", [wanted])

def find_user_row(ws, email: str) -> Optional[int]:
    email = email.strip().lower()
    values = ws.get_all_values()
    # row 1 = header
    for i in range(2, len(values) + 1):
        row = values[i-1]
        if len(row) >= 1 and row[0].strip().lower() == email:
            return i
    return None

def get_user_record(ws, email: str) -> Optional[Dict[str, Any]]:
    row = find_user_row(ws, email)
    if not row:
        return None
    vals = ws.row_values(row)
    # pad
    while len(vals) < 6:
        vals.append("")
    return {
        "row": row,
        "email": vals[0].strip().lower(),
        "password_hash": vals[1].strip(),
        "ativo": (vals[2].strip() or "1"),
        "created_at": vals[3].strip(),
        "sheet_id": vals[4].strip(),
        "sheet_tab": (vals[5].strip() or DEFAULT_USER_TAB),
    }


# =========================
# USER SHEET CREATION
# =========================
def ensure_user_sheet_headers(sheet_id: str, tab_name: str):
    gc = get_client()
    sh = gc.open_by_key(sheet_id)

    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=2000, cols=12)

    header = ws.row_values(1)
    wanted = ["Tipo", "Categoria", "Descrição", "Valor", "Data", "CreatedAt"]
    if [h.strip() for h in header[:6]] != wanted:
        ws.update("A1:F1", [wanted])

def create_user_spreadsheet(email: str) -> str:
    """
    Cria uma planilha nova para o usuário.
    Se TEMPLATE_SHEET_ID existir: copia o template.
    Senão: cria do zero com aba DEFAULT_USER_TAB e cabeçalho.
    Retorna o sheet_id criado.
    """
    gc = get_client()
    title = f"FinanceAI - {email}"

    if TEMPLATE_SHEET_ID:
        # copia template
        tpl = gc.open_by_key(TEMPLATE_SHEET_ID)
        new_file = gc.copy(tpl.id, title=title)
        sheet_id = new_file["id"]
    else:
        sh = gc.create(title)
        sheet_id = sh.id

    # mover para pasta se informado
    if DRIVE_FOLDER_ID:
        try:
            # gspread usa drive API via client (interno). Nem sempre expõe move direto,
            # então usamos request pela lib do google se necessário.
            # Para manter simples: apenas tenta setar parents via drive API REST não está disponível aqui.
            # (Na prática, sem isso, a planilha fica no "Meu Drive" do service account.)
            pass
        except:
            pass

    # garante cabeçalhos
    ensure_user_sheet_headers(sheet_id, DEFAULT_USER_TAB)
    return sheet_id


# =========================
# READ USER CONTEXT
# =========================
def get_current_user_sheet() -> Tuple[str, str]:
    sheet_id = (session.get("sheet_id") or "").strip()
    sheet_tab = (session.get("sheet_tab") or DEFAULT_USER_TAB).strip()
    if not sheet_id:
        raise RuntimeError("Usuário logado sem sheet_id na sessão.")
    return sheet_id, sheet_tab

def open_user_ws(sheet_id: str, tab: str):
    gc = get_client()
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(tab)
    return ws


# =========================
# ROUTES: AUTH
# =========================
@app.get("/me")
def me():
    if not is_logged():
        return jsonify({"ok": False}), 200
    return jsonify({
        "ok": True,
        "email": session.get("user_email"),
        "is_admin": is_admin()
    }), 200

@app.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()

    if not email or not password:
        return jsonify({"ok": False, "msg": "Informe e-mail e senha"}), 400

    # Admin login pelo hash do env
    if email == APP_EMAIL:
        if not APP_PASSWORD_HASH:
            return jsonify({"ok": False, "msg": "APP_PASSWORD_HASH não configurado"}), 500
        if not check_password_hash(APP_PASSWORD_HASH, password):
            return jsonify({"ok": False, "msg": "Credenciais inválidas"}), 401

        session["user_email"] = email
        session["is_admin"] = True
        # admin pode não ter planilha específica; mas se quiser, pode ter também no users sheet.
        # aqui, mantemos sem sheet para admin (ele não lança, só administra usuários).
        session.pop("sheet_id", None)
        session.pop("sheet_tab", None)
        return jsonify({"ok": True}), 200

    # Usuários normais vêm do MASTER_SHEET_ID (aba Usuarios)
    ws = open_users_ws()
    ensure_users_header(ws)
    rec = get_user_record(ws, email)
    if not rec:
        return jsonify({"ok": False, "msg": "Usuário não encontrado"}), 401
    if str(rec["ativo"]).strip() not in ("1", "true", "True", "SIM", "sim", "Ativo", "ativo"):
        return jsonify({"ok": False, "msg": "Usuário inativo"}), 403
    if not rec["password_hash"] or not check_password_hash(rec["password_hash"], password):
        return jsonify({"ok": False, "msg": "Credenciais inválidas"}), 401
    if not rec["sheet_id"]:
        return jsonify({"ok": False, "msg": "Usuário sem planilha vinculada (sheet_id vazio)"}), 500

    session["user_email"] = email
    session["is_admin"] = False
    session["sheet_id"] = rec["sheet_id"]
    session["sheet_tab"] = rec["sheet_tab"] or DEFAULT_USER_TAB
    return jsonify({"ok": True}), 200

@app.post("/logout")
def logout():
    session.clear()
    return jsonify({"ok": True}), 200


# =========================
# ROUTE: CREATE USER (ADMIN)
# =========================
@app.post("/create_user")
def create_user():
    guard = require_admin()
    if guard:
        return guard

    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = (data.get("password") or "").strip()

    if not email or not password:
        return jsonify({"ok": False, "msg": "Informe e-mail e senha"}), 400
    if "@" not in email:
        return jsonify({"ok": False, "msg": "E-mail inválido"}), 400

    ws = open_users_ws()
    ensure_users_header(ws)

    if find_user_row(ws, email):
        return jsonify({"ok": False, "msg": "Usuário já existe"}), 409

    # 1) cria planilha do usuário automaticamente
    sheet_id = create_user_spreadsheet(email)
    sheet_tab = DEFAULT_USER_TAB

    # 2) hash da senha
    pwd_hash = generate_password_hash(password, method="pbkdf2:sha256", salt_length=16)

    # 3) salva na aba Usuarios
    ws.append_row([email, pwd_hash, "1", now_iso(), sheet_id, sheet_tab], value_input_option="RAW")

    return jsonify({
        "ok": True,
        "msg": "Usuário criado e planilha gerada!",
        "sheet_id": sheet_id,
        "sheet_tab": sheet_tab
    }), 200


# =========================
# FINANCE: CRUD
# =========================
@app.post("/lancar")
def lancar():
    guard = require_login()
    if guard:
        return guard

    if is_admin():
        return jsonify({"ok": False, "msg": "Admin não lança. Faça login com usuário normal."}), 403

    data = request.get_json(silent=True) or {}
    tipo = (data.get("tipo") or "").strip()
    categoria = (data.get("categoria") or "").strip()
    descricao = (data.get("descricao") or "").strip()
    valor = data.get("valor")
    data_br = (data.get("data") or "").strip()  # dd/mm/aaaa

    if not tipo or tipo not in ("Gasto", "Receita"):
        return jsonify({"ok": False, "msg": "Tipo inválido"}), 400
    if not categoria or not descricao:
        return jsonify({"ok": False, "msg": "Informe categoria e descrição"}), 400

    valor_num = valor if isinstance(valor, (int, float)) else parse_money_br(valor)
    if valor_num is None or valor_num <= 0:
        return jsonify({"ok": False, "msg": "Valor inválido"}), 400

    iso = br_date_to_iso(data_br) if data_br else None
    if not iso:
        # se não veio data, usa hoje
        iso = date.today().strftime("%Y-%m-%d")
        data_br = iso_to_br(iso)

    sheet_id, tab = get_current_user_sheet()
    ws = open_user_ws(sheet_id, tab)
    ensure_user_sheet_headers(sheet_id, tab)

    ws.append_row([tipo, categoria, descricao, float(valor_num), data_br, now_iso()], value_input_option="RAW")
    return jsonify({"ok": True}), 200


def _read_all_items_for_user(sheet_id: str, tab: str) -> List[Dict[str, Any]]:
    ws = open_user_ws(sheet_id, tab)
    rows = ws.get_all_values()
    if not rows or len(rows) < 2:
        return []

    header = rows[0]
    items = []
    for idx in range(2, len(rows) + 1):
        r = rows[idx - 1]
        # pad
        while len(r) < len(header):
            r.append("")
        obj = {header[i]: r[i] for i in range(len(header))}
        obj["_row"] = idx
        items.append(obj)
    return items


def _apply_filters(items: List[Dict[str, Any]], params: Dict[str, Any]) -> List[Dict[str, Any]]:
    month = int(params.get("month") or 0) or None
    year = int(params.get("year") or 0) or None
    tipo = (params.get("tipo") or "Todos").strip()
    q = (params.get("q") or "").strip().lower()
    order = (params.get("order") or "recent").strip()

    date_from = (params.get("date_from") or "").strip()   # YYYY-MM-DD
    date_to = (params.get("date_to") or "").strip()

    vmin = parse_money_br(params.get("value_min")) if params.get("value_min") else None
    vmax = parse_money_br(params.get("value_max")) if params.get("value_max") else None

    df_iso = date_from if date_from else None
    dt_iso = date_to if date_to else None

    def in_month_year(item) -> bool:
        dbr = item.get("Data", "")
        iso = br_date_to_iso(dbr) or ""
        if not iso:
            return False
        m, y = month_year_from_iso(iso)
        if month and m != month:
            return False
        if year and y != year:
            return False
        return True

    def in_range(item) -> bool:
        dbr = item.get("Data", "")
        iso = br_date_to_iso(dbr)
        if not iso:
            return False
        if df_iso and iso < df_iso:
            return False
        if dt_iso and iso > dt_iso:
            return False
        return True

    def match_tipo(item) -> bool:
        if tipo == "Todos":
            return True
        return (item.get("Tipo", "") or "").strip() == tipo

    def match_q(item) -> bool:
        if not q:
            return True
        blob = " ".join([
            str(item.get("Tipo", "")),
            str(item.get("Categoria", "")),
            str(item.get("Descrição", "")),
            str(item.get("Data", "")),
            str(item.get("Valor", "")),
        ]).lower()
        return q in blob

    def match_val(item) -> bool:
        v = parse_money_br(item.get("Valor"))
        if v is None:
            return False
        if vmin is not None and v < vmin:
            return False
        if vmax is not None and v > vmax:
            return False
        return True

    out = []
    for it in items:
        if month or year:
            if not in_month_year(it):
                continue
        if (df_iso or dt_iso) and not in_range(it):
            continue
        if not match_tipo(it):
            continue
        if not match_q(it):
            continue
        if (vmin is not None or vmax is not None) and not match_val(it):
            continue
        out.append(it)

    # sorting
    def key_date(it):
        iso = br_date_to_iso(it.get("Data", "") or "") or "0000-00-00"
        return iso

    def key_val(it):
        return parse_money_br(it.get("Valor")) or 0

    if order == "recent":
        out.sort(key=key_date, reverse=True)
    elif order == "oldest":
        out.sort(key=key_date, reverse=False)
    elif order == "value_desc":
        out.sort(key=key_val, reverse=True)
    elif order == "value_asc":
        out.sort(key=key_val, reverse=False)

    return out


@app.get("/ultimos")
def ultimos():
    guard = require_login()
    if guard:
        return guard
    if is_admin():
        return jsonify({"ok": False, "msg": "Admin não possui lançamentos"}), 403

    sheet_id, tab = get_current_user_sheet()
    items = _read_all_items_for_user(sheet_id, tab)

    params = dict(request.args)
    items = _apply_filters(items, params)

    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 10))
    page = max(1, page)
    limit = max(1, min(500, limit))

    total = len(items)
    start = (page - 1) * limit
    end = start + limit
    page_items = items[start:end]

    return jsonify({"ok": True, "total": total, "items": page_items}), 200


@app.get("/resumo")
def resumo():
    guard = require_login()
    if guard:
        return guard
    if is_admin():
        return jsonify({"ok": False, "msg": "Admin não possui lançamentos"}), 403

    sheet_id, tab = get_current_user_sheet()
    items = _read_all_items_for_user(sheet_id, tab)

    params = dict(request.args)
    items = _apply_filters(items, params)

    entradas = 0.0
    saidas = 0.0

    # séries por dia do mês selecionado (para gráfico)
    month = int(request.args.get("month", datetime.now().month))
    year = int(request.args.get("year", datetime.now().year))

    # dias do mês (1..31) conforme calendário real:
    from calendar import monthrange
    _, last_day = monthrange(year, month)

    dias_labels = [str(d).zfill(2) for d in range(1, last_day + 1)]
    serie_receita = [0.0] * last_day
    serie_gasto = [0.0] * last_day

    gastos_cat: Dict[str, float] = {}
    receitas_cat: Dict[str, float] = {}

    for it in items:
        tipo = (it.get("Tipo") or "").strip()
        cat = (it.get("Categoria") or "Sem categoria").strip() or "Sem categoria"
        v = parse_money_br(it.get("Valor")) or 0.0
        iso = br_date_to_iso(it.get("Data", "") or "")
        if iso:
            try:
                y, m, d = iso.split("-")
                if int(y) == year and int(m) == month:
                    di = int(d)
                    if 1 <= di <= last_day:
                        if tipo == "Receita":
                            serie_receita[di - 1] += v
                        elif tipo == "Gasto":
                            serie_gasto[di - 1] += v
            except:
                pass

        if tipo == "Receita":
            entradas += v
            receitas_cat[cat] = receitas_cat.get(cat, 0.0) + v
        else:
            saidas += v
            gastos_cat[cat] = gastos_cat.get(cat, 0.0) + v

    saldo = entradas - saidas

    # pizza: top categorias
    def top_pairs(dct: Dict[str, float], topn=12):
        pairs = sorted(dct.items(), key=lambda x: x[1], reverse=True)
        return pairs[:topn]

    top_g = top_pairs(gastos_cat)
    top_r = top_pairs(receitas_cat)

    return jsonify({
        "ok": True,
        "entradas": entradas,
        "saidas": saidas,
        "saldo": saldo,
        "dias": dias_labels,
        "serie_receita": serie_receita,
        "serie_gasto": serie_gasto,
        "pizza_gastos_labels": [k for k, _ in top_g],
        "pizza_gastos_values": [v for _, v in top_g],
        "pizza_receitas_labels": [k for k, _ in top_r],
        "pizza_receitas_values": [v for _, v in top_r],
        "gastos_categorias": [{"categoria": k, "total": v} for k, v in top_g],
        "receitas_categorias": [{"categoria": k, "total": v} for k, v in top_r],
    }), 200


@app.patch("/lancamento/<int:row>")
def editar(row: int):
    guard = require_login()
    if guard:
        return guard
    if is_admin():
        return jsonify({"ok": False, "msg": "Admin não edita lançamentos"}), 403
    if row < 2:
        return jsonify({"ok": False, "msg": "Linha inválida"}), 400

    data = request.get_json(silent=True) or {}
    tipo = (data.get("tipo") or "").strip()
    categoria = (data.get("categoria") or "").strip()
    descricao = (data.get("descricao") or "").strip()
    valor = data.get("valor")
    data_br = (data.get("data") or "").strip()

    if tipo not in ("Gasto", "Receita"):
        return jsonify({"ok": False, "msg": "Tipo inválido"}), 400
    if not categoria or not descricao or not data_br:
        return jsonify({"ok": False, "msg": "Preencha categoria, descrição e data"}), 400

    valor_num = valor if isinstance(valor, (int, float)) else parse_money_br(valor)
    if valor_num is None or valor_num <= 0:
        return jsonify({"ok": False, "msg": "Valor inválido"}), 400
    if not br_date_to_iso(data_br):
        return jsonify({"ok": False, "msg": "Data inválida. Use dd/mm/aaaa"}), 400

    sheet_id, tab = get_current_user_sheet()
    ws = open_user_ws(sheet_id, tab)

    # Atualiza colunas A..E (mantém CreatedAt em F)
    ws.update(f"A{row}:E{row}", [[tipo, categoria, descricao, float(valor_num), data_br]])
    return jsonify({"ok": True}), 200


@app.delete("/lancamento/<int:row>")
def deletar(row: int):
    guard = require_login()
    if guard:
        return guard
    if is_admin():
        return jsonify({"ok": False, "msg": "Admin não exclui lançamentos"}), 403
    if row < 2:
        return jsonify({"ok": False, "msg": "Linha inválida"}), 400

    sheet_id, tab = get_current_user_sheet()
    ws = open_user_ws(sheet_id, tab)
    ws.delete_rows(row)
    return jsonify({"ok": True}), 200


# =========================
# EXPORT CSV/PDF
# =========================
@app.get("/export.csv")
def export_csv():
    guard = require_login()
    if guard:
        return guard
    if is_admin():
        return jsonify({"ok": False, "msg": "Admin não exporta lançamentos"}), 403

    sheet_id, tab = get_current_user_sheet()
    items = _read_all_items_for_user(sheet_id, tab)
    params = dict(request.args)
    items = _apply_filters(items, params)

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Tipo", "Categoria", "Descrição", "Valor", "Data"])
    for it in items:
        writer.writerow([
            it.get("Tipo", ""),
            it.get("Categoria", ""),
            it.get("Descrição", ""),
            it.get("Valor", ""),
            it.get("Data", ""),
        ])
    output.seek(0)

    mem = io.BytesIO(output.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="finance-ai.csv")


@app.get("/export.pdf")
def export_pdf():
    guard = require_login()
    if guard:
        return guard
    if is_admin():
        return jsonify({"ok": False, "msg": "Admin não exporta lançamentos"}), 403

    sheet_id, tab = get_current_user_sheet()
    items = _read_all_items_for_user(sheet_id, tab)
    params = dict(request.args)
    items = _apply_filters(items, params)

    mem = io.BytesIO()
    c = canvas.Canvas(mem, pagesize=A4)
    w, h = A4

    y = h - 50
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, "Finance AI — Exportação")
    y -= 22

    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"Usuário: {session.get('user_email')}")
    y -= 18
    c.drawString(40, y, f"Gerado em: {now_iso()}")
    y -= 28

    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "Tipo")
    c.drawString(105, y, "Categoria")
    c.drawString(235, y, "Descrição")
    c.drawString(430, y, "Valor")
    c.drawString(485, y, "Data")
    y -= 12
    c.line(40, y, w - 40, y)
    y -= 14

    c.setFont("Helvetica", 9)

    def clip(s: str, n: int) -> str:
        s = str(s or "")
        return s if len(s) <= n else s[: n - 1] + "…"

    for it in items:
        if y < 60:
            c.showPage()
            y = h - 50
            c.setFont("Helvetica-Bold", 10)
            c.drawString(40, y, "Tipo")
            c.drawString(105, y, "Categoria")
            c.drawString(235, y, "Descrição")
            c.drawString(430, y, "Valor")
            c.drawString(485, y, "Data")
            y -= 12
            c.line(40, y, w - 40, y)
            y -= 14
            c.setFont("Helvetica", 9)

        c.drawString(40, y, clip(it.get("Tipo", ""), 10))
        c.drawString(105, y, clip(it.get("Categoria", ""), 18))
        c.drawString(235, y, clip(it.get("Descrição", ""), 36))
        c.drawRightString(470, y, clip(it.get("Valor", ""), 12))
        c.drawString(485, y, clip(it.get("Data", ""), 10))
        y -= 14

    c.save()
    mem.seek(0)
    return send_file(mem, mimetype="application/pdf", as_attachment=True, download_name="finance-ai.pdf")


# =========================
# HEALTH
# =========================
@app.get("/health")
def health():
    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
