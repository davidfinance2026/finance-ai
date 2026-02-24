import os
import csv
import io
import secrets
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from functools import wraps


def require_login(fn=None):
    """Decorator para proteger rotas. Use como @require_login ou @require_login()."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("user_email"):
                # Para chamadas de API, retorna JSON; para páginas, redireciona
                if request.path.startswith("/api/"):
                    return jsonify({"ok": False, "error": "not_authenticated"}), 401
                return redirect(url_for("index", _external=False) + "?login=1")
            return f(*args, **kwargs)
        return wrapper

    if callable(fn):
        return decorator(fn)
    return decorator


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
RESETS_TAB = os.getenv("RESETS_TAB", "PasswordResets").strip()
INVEST_TAB = os.getenv("INVEST_TAB", "Investimentos").strip()  # ✅ NOVO

FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret").strip()

# Admin bootstrap via env
APP_EMAIL = os.getenv("APP_EMAIL", "").strip()
APP_PASSWORD_HASH = os.getenv("APP_PASSWORD_HASH", "").strip()

# credentials: 1) JSON env 2) secret file 3) local file
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()
SECRET_FILE_PATH = "/etc/secrets/google_creds.json"

# IMPORTANT: in localhost (http), secure cookie breaks sessions
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1").strip().lower() in ("1", "true", "sim", "yes")

# Forgot password behavior
RESET_CODE_TTL_MIN = int(os.getenv("RESET_CODE_TTL_MIN", "15"))
RESET_RETURN_CODE = os.getenv("RESET_RETURN_CODE", "0").strip().lower() in ("1", "true", "sim", "yes")
# ^ se TRUE, o /forgot_password devolve o código no JSON (ótimo p/ dev). Em prod deixe 0.

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

USERS_HEADERS_NEW = ["NomeApelido", "NomeCompleto", "Telefone", "Email", "PasswordHash", "Ativo", "CreatedAt"]
USERS_HEADERS_OLD = ["Email", "PasswordHash", "Ativo", "CreatedAt"]

RESETS_HEADERS = ["Email", "CodeHash", "ExpiresAt", "CreatedAt", "UsedAt"]

INVEST_HEADERS = [
    "Email",
    "Tipo",         # Aporte | Retirada
    "Ativo",        # Ex: CDB, Tesouro Selic, PETR4, BTC...
    "Instituicao",  # Ex: Nubank, Inter, XP...
    "Descricao",    # livre
    "Valor",        # sempre número string "123.45"
    "Data",         # dd/mm/aaaa
    "CreatedAt"
]


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
    # Aqui NÃO apagamos automaticamente se for Users — para Users faremos migração segura.
    if title != USERS_TAB and [h.strip() for h in existing] != headers:
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


def require_login_guard():
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


def match_query(item: Dict[str, Any], q: str, keys: Optional[List[str]] = None) -> bool:
    if not q:
        return True
    ql = q.lower()
    if not keys:
        keys = ["Tipo", "Categoria", "Descrição", "Data", "Valor"]
    hay = " ".join([str(item.get(k, "")) for k in keys]).lower()
    return ql in hay


def item_value_num(item: Dict[str, Any], key: str = "Valor") -> float:
    return money_to_float(item.get(key, 0))


def item_date_num(item: Dict[str, Any], key: str = "Data") -> int:
    d = parse_date_br(str(item.get(key, "")))
    if not d:
        return 0
    return int(d.strftime("%Y%m%d"))


def normalize_phone_br(raw: str) -> Optional[str]:
    digits = "".join([c for c in str(raw or "") if c.isdigit()])
    if len(digits) not in (10, 11):
        return None

    ddd = digits[:2]
    rest = digits[2:]

    if len(rest) == 8:  # fixo
        return f"({ddd}) {rest[:4]}-{rest[4:]}"
    # móvel (9XXXX-XXXX)
    return f"({ddd}) {rest[:5]}-{rest[5:]}"


def utc_plus_minutes_str(minutes: int) -> str:
    return (datetime.utcnow() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def parse_utc_str(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


# =========================
# STATIC (PWA) - correct MIME
# =========================
@app.route("/static/<path:filename>")
def static_files(filename):
    resp = send_from_directory(app.static_folder, filename)

    if filename.endswith("manifest.json"):
        resp.headers["Content-Type"] = "application/manifest+json; charset=utf-8"
    elif filename.endswith(".json"):
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
    elif filename.endswith(".js"):
        resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    return resp


# ✅ route /sw.js at root (best practice for PWA scope)
@app.route("/sw.js")
def sw_root():
    resp = send_from_directory(app.static_folder, "sw.js")
    resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    return resp


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
    """
    ✅ Migração automática e segura:
    - Se header já é novo => ok
    - Se header é antigo => migrar linhas para novo schema
    - Se header desconhecido => tentar mapear sem apagar (se não der, levanta erro)
    """
    spread = open_spread()

    try:
        ws = spread.worksheet(USERS_TAB)
    except gspread.WorksheetNotFound:
        ws = spread.add_worksheet(title=USERS_TAB, rows=2000, cols=max(10, len(USERS_HEADERS_NEW)))
        ws.append_row(USERS_HEADERS_NEW, value_input_option="RAW")
        return ws

    existing = [h.strip() for h in (ws.row_values(1) or [])]
    if existing == USERS_HEADERS_NEW:
        return ws

    # Caso antigo: migrar sem perder usuários
    if existing == USERS_HEADERS_OLD:
        values = ws.get_all_values()
        old_rows = values[1:] if len(values) > 1 else []

        ws.clear()
        ws.append_row(USERS_HEADERS_NEW, value_input_option="RAW")

        for row in old_rows:
            email = (row[0] if len(row) > 0 else "").strip()
            ph = (row[1] if len(row) > 1 else "").strip()
            ativo = (row[2] if len(row) > 2 else "1").strip() or "1"
            created = (row[3] if len(row) > 3 else now_str()).strip() or now_str()

            nome_apelido = (email.split("@")[0] if email else "Usuário")[:40]
            nome_completo = ""
            telefone = "(00) 00000-0000"

            ws.append_row([nome_apelido, nome_completo, telefone, email, ph, ativo, created], value_input_option="RAW")
        return ws

    # Header “custom”: tenta mapear se tiver Email e PasswordHash
    if "Email" in existing and "PasswordHash" in existing:
        values = ws.get_all_values()
        if not values:
            ws.clear()
            ws.append_row(USERS_HEADERS_NEW, value_input_option="RAW")
            return ws

        headers = existing
        data_rows = values[1:] if len(values) > 1 else []

        def idx(name: str) -> int:
            try:
                return headers.index(name)
            except ValueError:
                return -1

        i_email = idx("Email")
        i_ph = idx("PasswordHash")
        i_ativo = idx("Ativo")
        i_created = idx("CreatedAt")
        i_nick = idx("NomeApelido")
        i_full = idx("NomeCompleto")
        i_phone = idx("Telefone")

        ws.clear()
        ws.append_row(USERS_HEADERS_NEW, value_input_option="RAW")

        for r in data_rows:
            email = (r[i_email] if i_email >= 0 and i_email < len(r) else "").strip().lower()
            ph = (r[i_ph] if i_ph >= 0 and i_ph < len(r) else "").strip()
            ativo = (r[i_ativo] if i_ativo >= 0 and i_ativo < len(r) else "1").strip() or "1"
            created = (r[i_created] if i_created >= 0 and i_created < len(r) else now_str()).strip() or now_str()

            nome_apelido = (r[i_nick] if i_nick >= 0 and i_nick < len(r) else "") or (email.split("@")[0] if email else "Usuário")
            nome_completo = (r[i_full] if i_full >= 0 and i_full < len(r) else "")
            telefone = (r[i_phone] if i_phone >= 0 and i_phone < len(r) else "") or "(00) 00000-0000"

            ws.append_row([nome_apelido, nome_completo, telefone, email, ph, ativo, created], value_input_option="RAW")

        return ws

    raise RuntimeError(f"Aba {USERS_TAB} com headers inesperados: {existing}")


def lanc_ws():
    spread = open_spread()
    ws = ensure_worksheet(spread, SHEET_TAB, headers=["Email", "Tipo", "Categoria", "Descrição", "Valor", "Data", "CreatedAt"])
    existing = [h.strip() for h in (ws.row_values(1) or [])]
    if existing != ["Email", "Tipo", "Categoria", "Descrição", "Valor", "Data", "CreatedAt"]:
        ws.clear()
        ws.append_row(["Email", "Tipo", "Categoria", "Descrição", "Valor", "Data", "CreatedAt"], value_input_option="RAW")
    return ws


def metas_ws():
    spread = open_spread()
    ws = ensure_worksheet(spread, METAS_TAB, headers=["Email", "Mes", "Ano", "MetaReceitas", "MetaGastos", "CreatedAt", "UpdatedAt"])
    existing = [h.strip() for h in (ws.row_values(1) or [])]
    if existing != ["Email", "Mes", "Ano", "MetaReceitas", "MetaGastos", "CreatedAt", "UpdatedAt"]:
        ws.clear()
        ws.append_row(["Email", "Mes", "Ano", "MetaReceitas", "MetaGastos", "CreatedAt", "UpdatedAt"], value_input_option="RAW")
    return ws


def resets_ws():
    spread = open_spread()
    ws = ensure_worksheet(spread, RESETS_TAB, headers=RESETS_HEADERS)
    existing = [h.strip() for h in (ws.row_values(1) or [])]
    if existing != RESETS_HEADERS:
        ws.clear()
        ws.append_row(RESETS_HEADERS, value_input_option="RAW")
    return ws


def invest_ws():
    spread = open_spread()
    ws = ensure_worksheet(spread, INVEST_TAB, headers=INVEST_HEADERS)
    existing = [h.strip() for h in (ws.row_values(1) or [])]
    if existing != INVEST_HEADERS:
        ws.clear()
        ws.append_row(INVEST_HEADERS, value_input_option="RAW")
    return ws


def ensure_admin_bootstrap():
    if not APP_EMAIL or not APP_PASSWORD_HASH:
        return

    ws = users_ws()
    recs = get_records(ws)
    for r in recs:
        if safe_lower(r.get("Email")) == safe_lower(APP_EMAIL):
            return

    ws.append_row(
        ["Admin", "Administrador", "(00) 00000-0000", APP_EMAIL, APP_PASSWORD_HASH, "1", now_str()],
        value_input_option="RAW"
    )


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
    if ativo not in ("1", "true", "True", "SIM", "sim", "yes", "Yes"):
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
# SELF SIGNUP (SEM ADMIN)
# =========================
@app.route("/signup", methods=["POST"])
def signup():
    ensure_admin_bootstrap()

    data = request.get_json(silent=True) or {}

    nome_apelido = str(data.get("nome_apelido", "")).strip()
    nome_completo = str(data.get("nome_completo", "")).strip()
    telefone_raw = str(data.get("telefone", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", "")).strip()
    confirm_password = str(data.get("confirm_password", "")).strip()

    if not nome_apelido or not nome_completo or not telefone_raw or not email or not password or not confirm_password:
        return jsonify({"ok": False, "msg": "Preencha todos os campos."}), 400

    if "@" not in email or "." not in email:
        return jsonify({"ok": False, "msg": "E-mail inválido."}), 400

    if password != confirm_password:
        return jsonify({"ok": False, "msg": "As senhas não conferem."}), 400

    if len(password) < 6:
        return jsonify({"ok": False, "msg": "Senha muito curta (mín. 6 caracteres)."}), 400

    telefone = normalize_phone_br(telefone_raw)
    if not telefone:
        return jsonify({"ok": False, "msg": "Telefone inválido. Use DDD + número (10 ou 11 dígitos)."}), 400

    ws = users_ws()
    recs = get_records(ws)
    for r in recs:
        if safe_lower(r.get("Email")) == email:
            return jsonify({"ok": False, "msg": "Este e-mail já está cadastrado."}), 400

    ph = generate_password_hash(password, method="pbkdf2:sha256", salt_length=16)
    ws.append_row([nome_apelido, nome_completo, telefone, email, ph, "1", now_str()], value_input_option="RAW")

    session["user_email"] = email
    return jsonify({"ok": True, "msg": "Cadastro criado com sucesso!", "email": email})


# =========================
# FORGOT / RESET PASSWORD
# =========================
@app.route("/forgot_password", methods=["POST"])
def forgot_password():
    ensure_admin_bootstrap()

    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    if not email:
        return jsonify({"ok": False, "msg": "Informe o e-mail."}), 400

    uws = users_ws()
    recs = get_records(uws)
    user = None
    for r in recs:
        if safe_lower(r.get("Email")) == email:
            user = r
            break
    if not user:
        return jsonify({"ok": False, "msg": "Se o e-mail existir, enviaremos o código."}), 200

    ativo = str(user.get("Ativo", "1")).strip()
    if ativo not in ("1", "true", "True", "SIM", "sim", "yes", "Yes"):
        return jsonify({"ok": False, "msg": "Usuário inativo."}), 403

    code = f"{secrets.randbelow(10**6):06d}"
    code_hash = generate_password_hash(code, method="pbkdf2:sha256", salt_length=16)

    rws = resets_ws()
    expires_at = utc_plus_minutes_str(RESET_CODE_TTL_MIN)
    rws.append_row([email, code_hash, expires_at, now_str(), ""], value_input_option="RAW")

    payload = {"ok": True, "msg": "Se o e-mail existir, enviaremos o código de redefinição."}
    if RESET_RETURN_CODE:
        payload["dev_code"] = code
        payload["dev_expires_at"] = expires_at
    return jsonify(payload)


def _find_latest_valid_reset_row(ws, email: str) -> Optional[Dict[str, Any]]:
    recs = get_records(ws)
    recs.sort(key=lambda x: (str(x.get("CreatedAt", ""))), reverse=True)
    for r in recs:
        if safe_lower(r.get("Email")) != safe_lower(email):
            continue
        if str(r.get("UsedAt", "")).strip():
            continue
        exp = parse_utc_str(r.get("ExpiresAt", ""))
        if not exp:
            continue
        if datetime.utcnow() > exp:
            continue
        return r
    return None


@app.route("/reset_password", methods=["POST"])
def reset_password():
    ensure_admin_bootstrap()

    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    code = str(data.get("code", "")).strip()
    new_password = str(data.get("new_password", "")).strip()
    confirm_password = str(data.get("confirm_password", "")).strip()

    if not email or not code or not new_password or not confirm_password:
        return jsonify({"ok": False, "msg": "Informe e-mail, código e nova senha."}), 400

    if new_password != confirm_password:
        return jsonify({"ok": False, "msg": "As senhas não conferem."}), 400

    if len(new_password) < 6:
        return jsonify({"ok": False, "msg": "Senha muito curta (mín. 6 caracteres)."}), 400

    rws = resets_ws()
    rr = _find_latest_valid_reset_row(rws, email)
    if not rr:
        return jsonify({"ok": False, "msg": "Código inválido ou expirado."}), 400

    code_hash = str(rr.get("CodeHash", "")).strip()
    if not code_hash or not check_password_hash(code_hash, code):
        return jsonify({"ok": False, "msg": "Código inválido ou expirado."}), 400

    uws = users_ws()
    headers = uws.row_values(1)
    col = {h: i + 1 for i, h in enumerate(headers)}

    recs = get_records(uws)
    user_row = None
    for r in recs:
        if safe_lower(r.get("Email")) == safe_lower(email):
            user_row = int(r.get("_row", 0)) or None
            break
    if not user_row:
        return jsonify({"ok": False, "msg": "Usuário não encontrado."}), 404

    new_hash = generate_password_hash(new_password, method="pbkdf2:sha256", salt_length=16)
    uws.update_cell(user_row, col["PasswordHash"], new_hash)

    used_row = int(rr.get("_row", 0)) or None
    if used_row:
        rws.update_cell(used_row, 5, now_str())  # UsedAt

    session["user_email"] = email
    return jsonify({"ok": True, "msg": "Senha redefinida com sucesso!", "email": email})


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
    guard = require_login_guard()
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
    guard = require_login_guard()
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
# LANÇAR (receitas/gastos)
# =========================
@app.route("/lancar", methods=["POST"])
def lancar():
    guard = require_login_guard()
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
    ws.append_row([user_email, tipo, categoria, descricao, f"{valor:.2f}", data_br, now_str()], value_input_option="RAW")
    return jsonify({"ok": True})


# =========================
# INVESTIMENTOS (Modelo A)
# =========================
@app.route("/investir", methods=["POST"])
def investir():
    guard = require_login_guard()
    if guard:
        return jsonify(guard[0]), guard[1]

    user_email = session["user_email"]
    dataj = request.get_json(silent=True) or {}

    tipo = str(dataj.get("tipo", "")).strip()  # Aporte / Retirada
    ativo = str(dataj.get("ativo", "")).strip()
    instituicao = str(dataj.get("instituicao", "")).strip()
    descricao = str(dataj.get("descricao", "")).strip()
    valor = money_to_float(dataj.get("valor"))
    data_br = str(dataj.get("data", "")).strip()

    if tipo not in ("Aporte", "Retirada"):
        return jsonify({"ok": False, "msg": "Tipo inválido. Use Aporte ou Retirada"}), 400
    if not ativo:
        return jsonify({"ok": False, "msg": "Ativo é obrigatório"}), 400
    if valor <= 0:
        return jsonify({"ok": False, "msg": "Valor inválido"}), 400

    if data_br:
        d = parse_date_br(data_br)
        if not d:
            return jsonify({"ok": False, "msg": "Data inválida. Use dd/mm/aaaa"}), 400
    else:
        data_br = date.today().strftime("%d/%m/%Y")

    ws = invest_ws()
    ws.append_row(
        [user_email, tipo, ativo, instituicao, descricao, f"{valor:.2f}", data_br, now_str()],
        value_input_option="RAW"
    )
    return jsonify({"ok": True})


def apply_invest_filters(items: List[Dict[str, Any]], params: Dict[str, Any]) -> List[Dict[str, Any]]:
    month = params.get("month")
    year = params.get("year")
    tipo = params.get("tipo", "Todos")
    q = params.get("q", "")
    date_from = params.get("date_from", "")
    date_to = params.get("date_to", "")
    vmin = params.get("value_min", "")
    vmax = params.get("value_max", "")
    ativo = params.get("ativo", "")

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

        if ativo:
            if safe_lower(it.get("Ativo")) != safe_lower(ativo):
                continue

        if q and not match_query(it, q, keys=["Tipo", "Ativo", "Instituicao", "Descricao", "Data", "Valor"]):
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

        val = item_value_num(it, key="Valor")
        if vmin_n is not None and val < vmin_n:
            continue
        if vmax_n is not None and val > vmax_n:
            continue

        out.append(it)

    return out


@app.route("/investimentos")
def investimentos_list():
    guard = require_login_guard()
    if guard:
        return jsonify(guard[0]), guard[1]

    user_email = session["user_email"]
    ws = invest_ws()
    items = get_records(ws)
    items = [i for i in items if safe_lower(i.get("Email")) == safe_lower(user_email)]

    month = request.args.get("month", type=int)
    year = request.args.get("year", type=int)
    tipo = request.args.get("tipo", default="Todos", type=str)  # Todos | Aporte | Retirada
    q = request.args.get("q", default="", type=str)
    order = request.args.get("order", default="recent", type=str)
    date_from = request.args.get("date_from", default="", type=str)
    date_to = request.args.get("date_to", default="", type=str)
    value_min = request.args.get("value_min", default="", type=str)
    value_max = request.args.get("value_max", default="", type=str)
    ativo = request.args.get("ativo", default="", type=str)
    page = request.args.get("page", default=1, type=int)
    limit = request.args.get("limit", default=10, type=int)

    filtered = apply_invest_filters(items, {
        "month": month, "year": year, "tipo": tipo, "q": q,
        "date_from": date_from, "date_to": date_to,
        "value_min": value_min, "value_max": value_max,
        "ativo": ativo
    })

    if order == "oldest":
        filtered.sort(key=lambda x: (item_date_num(x, "Data"), x.get("_row", 0)))
    elif order == "value_desc":
        filtered.sort(key=lambda x: item_value_num(x, "Valor"), reverse=True)
    elif order == "value_asc":
        filtered.sort(key=lambda x: item_value_num(x, "Valor"))
    else:
        filtered.sort(key=lambda x: (item_date_num(x, "Data"), x.get("_row", 0)), reverse=True)

    total = len(filtered)
    limit = max(1, min(int(limit or 10), 200))
    page = max(1, int(page or 1))
    start = (page - 1) * limit
    end = start + limit
    page_items = filtered[start:end]

    return jsonify({"ok": True, "total": total, "items": page_items})


@app.route("/invest_resumo")
def invest_resumo():
    """
    Resumo simples: total aportado, total retirado, saldo líquido e saldo por ativo.
    """
    guard = require_login_guard()
    if guard:
        return jsonify(guard[0]), guard[1]

    user_email = session["user_email"]
    ws = invest_ws()
    items = get_records(ws)
    items = [i for i in items if safe_lower(i.get("Email")) == safe_lower(user_email)]

    month = request.args.get("month", type=int)
    year = request.args.get("year", type=int)
    q = request.args.get("q", default="", type=str)

    filtered = apply_invest_filters(items, {
        "month": month, "year": year,
        "tipo": "Todos",
        "q": q,
        "date_from": "", "date_to": "",
        "value_min": "", "value_max": "",
        "ativo": ""
    })

    aportes = 0.0
    retiradas = 0.0
    por_ativo: Dict[str, float] = {}

    for it in filtered:
        t = str(it.get("Tipo", "")).strip()
        ativo = str(it.get("Ativo", "")).strip() or "Sem ativo"
        val = item_value_num(it, "Valor")

        if t == "Aporte":
            aportes += val
            por_ativo[ativo] = por_ativo.get(ativo, 0.0) + val
        else:
            retiradas += val
            por_ativo[ativo] = por_ativo.get(ativo, 0.0) - val

    saldo = aportes - retiradas
    arr = [{"ativo": k, "saldo": v} for k, v in por_ativo.items()]
    arr.sort(key=lambda x: x["saldo"], reverse=True)

    return jsonify({
        "ok": True,
        "aportes": aportes,
        "retiradas": retiradas,
        "saldo": saldo,
        "por_ativo": arr[:50]
    })


# =========================
# EDITAR / EXCLUIR (investimentos)
# =========================
@app.route("/investimento/<int:row>", methods=["PATCH"])
def editar_investimento(row: int):
    guard = require_login_guard()
    if guard:
        return jsonify(guard[0]), guard[1]

    user_email = session["user_email"]
    if row < 2:
        return jsonify({"ok": False, "msg": "Linha inválida"}), 400

    dataj = request.get_json(silent=True) or {}
    tipo = str(dataj.get("tipo", "")).strip()
    ativo = str(dataj.get("ativo", "")).strip()
    instituicao = str(dataj.get("instituicao", "")).strip()
    descricao = str(dataj.get("descricao", "")).strip()
    valor = money_to_float(dataj.get("valor"))
    data_br = str(dataj.get("data", "")).strip()

    if tipo not in ("Aporte", "Retirada"):
        return jsonify({"ok": False, "msg": "Tipo inválido"}), 400
    if not ativo:
        return jsonify({"ok": False, "msg": "Ativo é obrigatório"}), 400
    if valor <= 0:
        return jsonify({"ok": False, "msg": "Valor inválido"}), 400
    if not data_br or not parse_date_br(data_br):
        return jsonify({"ok": False, "msg": "Data inválida. Use dd/mm/aaaa"}), 400

    ws = invest_ws()
    row_vals = ws.row_values(row)
    headers = ws.row_values(1)
    if not row_vals:
        return jsonify({"ok": False, "msg": "Investimento não encontrado"}), 404

    col = {h: idx + 1 for idx, h in enumerate(headers)}
    email_in_row = (row_vals[col["Email"] - 1] if "Email" in col and len(row_vals) >= col["Email"] else "").strip().lower()
    if email_in_row != user_email.lower():
        return jsonify({"ok": False, "msg": "Sem permissão"}), 403

    ws.update_cell(row, col["Tipo"], tipo)
    ws.update_cell(row, col["Ativo"], ativo)
    ws.update_cell(row, col["Instituicao"], instituicao)
    ws.update_cell(row, col["Descricao"], descricao)
    ws.update_cell(row, col["Valor"], f"{valor:.2f}")
    ws.update_cell(row, col["Data"], data_br)

    return jsonify({"ok": True})


@app.route("/investimento/<int:row>", methods=["DELETE"])
def excluir_investimento(row: int):
    guard = require_login_guard()
    if guard:
        return jsonify(guard[0]), guard[1]

    user_email = session["user_email"]
    if row < 2:
        return jsonify({"ok": False, "msg": "Linha inválida"}), 400

    ws = invest_ws()
    headers = ws.row_values(1)
    col = {h: idx + 1 for idx, h in enumerate(headers)}

    row_vals = ws.row_values(row)
    if not row_vals:
        return jsonify({"ok": False, "msg": "Investimento não encontrado"}), 404

    email_in_row = (row_vals[col["Email"] - 1] if "Email" in col and len(row_vals) >= col["Email"] else "").strip().lower()
    if email_in_row != user_email.lower():
        return jsonify({"ok": False, "msg": "Sem permissão"}), 403

    ws.delete_rows(row)
    return jsonify({"ok": True})


# =========================
# FILTERS + LIST (lançamentos)
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

        if q and not match_query(it, q, keys=["Tipo", "Categoria", "Descrição", "Data", "Valor"]):
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

        val = item_value_num(it, "Valor")
        if vmin_n is not None and val < vmin_n:
            continue
        if vmax_n is not None and val > vmax_n:
            continue

        out.append(it)

    return out


@app.route("/ultimos")
def ultimos():
    guard = require_login_guard()
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
        filtered.sort(key=lambda x: (item_date_num(x, "Data"), x.get("_row", 0)))
    elif order == "value_desc":
        filtered.sort(key=lambda x: item_value_num(x, "Valor"), reverse=True)
    elif order == "value_asc":
        filtered.sort(key=lambda x: item_value_num(x, "Valor"))
    else:
        filtered.sort(key=lambda x: (item_date_num(x, "Data"), x.get("_row", 0)), reverse=True)

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
        val = item_value_num(it, "Valor")
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
    guard = require_login_guard()
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


# =========================
# EDITAR / EXCLUIR (lançamentos)
# =========================
@app.route("/lancamento/<int:row>", methods=["PATCH"])
def editar_lancamento(row: int):
    guard = require_login_guard()
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
    guard = require_login_guard()
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
    guard = require_login_guard()
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
        filtered.sort(key=lambda x: (item_date_num(x, "Data"), x.get("_row", 0)))
    elif order == "value_desc":
        filtered.sort(key=lambda x: item_value_num(x, "Valor"), reverse=True)
    elif order == "value_asc":
        filtered.sort(key=lambda x: item_value_num(x, "Valor"))
    else:
        filtered.sort(key=lambda x: (item_date_num(x, "Data"), x.get("_row", 0)), reverse=True)

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
