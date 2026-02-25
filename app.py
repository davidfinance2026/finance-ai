import os
import json
from datetime import datetime, date
from functools import lru_cache

from flask import Flask, request, jsonify, session, redirect, url_for, render_template
from werkzeug.security import generate_password_hash, check_password_hash

import gspread
from google.oauth2.service_account import Credentials


# ---------------------------
# Flask config
# ---------------------------
app = Flask(__name__)

# Defina SECRET_KEY no Railway (Variables) para manter sessão estável
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")


# ---------------------------
# Google Sheets helpers
# ---------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _load_service_account_info() -> dict:
    raw = os.environ.get("SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("SERVICE_ACCOUNT_JSON não configurado nas Variables do Railway.")

    # Às vezes o JSON vem com aspas/escapes; tentamos carregar de forma robusta
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Tentativa extra: remover aspas externas e reparse
        raw2 = raw.strip()
        if (raw2.startswith('"') and raw2.endswith('"')) or (raw2.startswith("'") and raw2.endswith("'")):
            raw2 = raw2[1:-1]
        # Consertar escapes comuns
        raw2 = raw2.replace("\\n", "\n")
        return json.loads(raw2)


@lru_cache(maxsize=1)
def get_gspread_client() -> gspread.Client:
    info = _load_service_account_info()
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def open_spreadsheet(gc: gspread.Client):
    sheet_id = os.environ.get("SPREADSHEET_ID")
    sheet_name = os.environ.get("SPREADSHEET_NAME", "Controle Financeiro")

    if sheet_id:
        return gc.open_by_key(sheet_id)
    return gc.open(sheet_name)


def _ensure_worksheet(sh, title: str, headers: list[str]):
    """
    Garante que a aba exista e tenha cabeçalho na primeira linha.
    """
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=2000, cols=max(10, len(headers)))

    # Se estiver vazia, cria cabeçalho
    values = ws.get_all_values()
    if not values:
        ws.append_row(headers)

    # Se tiver algo, mas cabeçalho errado/vazio, tenta corrigir a primeira linha
    else:
        first = values[0]
        if not first or all((c.strip() == "" for c in first)):
            ws.update("A1", [headers])

    return ws


def ensure_schema():
    gc = get_gspread_client()
    sh = open_spreadsheet(gc)

    usuarios_headers = [
        "email",
        "senha_hash",
        "nome_apelido",
        "nome_completo",
        "telefone",
        "criado_em",
    ]
    lanc_headers = [
        "user_email",
        "data",          # YYYY-MM-DD
        "tipo",          # RECEITA / GASTO
        "categoria",
        "descricao",
        "valor",         # numero
        "criado_em",
    ]
    inv_headers = [
        "user_email",
        "data",
        "ativo",
        "quantidade",
        "preco",
        "observacao",
        "criado_em",
    ]

    ws_users = _ensure_worksheet(sh, "Usuarios", usuarios_headers)
    ws_lanc = _ensure_worksheet(sh, "Lancamentos", lanc_headers)
    ws_inv = _ensure_worksheet(sh, "Investimentos", inv_headers)

    return sh, ws_users, ws_lanc, ws_inv


def _now_iso():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _require_login():
    if "user_email" not in session:
        return False
    return True


# ---------------------------
# Pages (se você usa templates)
# ---------------------------
@app.get("/")
def home():
    # Se você tiver templates/index.html, ele vai renderizar.
    # Se não tiver, comenta esta linha e retorna algo simples.
    try:
        return render_template("index.html")
    except Exception:
        return "FinanceAI online ✅"


# ---------------------------
# Auth APIs
# ---------------------------
@app.post("/api/register")
def api_register():
    """
    Espera JSON:
    {
      "nome_apelido": "...",
      "nome_completo": "...",
      "telefone": "...",
      "email": "...",
      "senha": "...",
      "confirmar_senha": "..."
    }
    """
    data = request.get_json(silent=True) or {}
    nome_apelido = (data.get("nome_apelido") or "").strip()
    nome_completo = (data.get("nome_completo") or "").strip()
    telefone = (data.get("telefone") or "").strip()
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha") or ""
    confirmar = data.get("confirmar_senha") or ""

    if not email or not senha:
        return jsonify({"ok": False, "error": "Informe e-mail e senha."}), 400
    if senha != confirmar:
        return jsonify({"ok": False, "error": "As senhas não conferem."}), 400
    if len(senha) < 6:
        return jsonify({"ok": False, "error": "A senha deve ter pelo menos 6 caracteres."}), 400

    sh, ws_users, _, _ = ensure_schema()

    # Busca por email existente
    try:
        emails = ws_users.col_values(1)  # coluna A = email
    except Exception as e:
        return jsonify({"ok": False, "error": f"Erro ao acessar aba Usuarios: {str(e)}"}), 500

    if email in [e.strip().lower() for e in emails[1:]]:  # ignora cabeçalho
        return jsonify({"ok": False, "error": "Este e-mail já está cadastrado."}), 409

    senha_hash = generate_password_hash(senha)
    ws_users.append_row([
        email,
        senha_hash,
        nome_apelido,
        nome_completo,
        telefone,
        _now_iso(),
    ])

    session["user_email"] = email
    return jsonify({"ok": True, "email": email})


@app.post("/api/login")
def api_login():
    """
    Espera JSON:
    { "email": "...", "senha": "..." }
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha") or ""

    if not email or not senha:
        return jsonify({"ok": False, "error": "Informe e-mail e senha."}), 400

    _, ws_users, _, _ = ensure_schema()
    rows = ws_users.get_all_values()
    if len(rows) <= 1:
        return jsonify({"ok": False, "error": "Nenhum usuário cadastrado ainda."}), 401

    header = rows[0]
    # Mapeia colunas
    idx = {name: header.index(name) for name in header if name in header}

    for r in rows[1:]:
        if len(r) < 2:
            continue
        r_email = (r[idx["email"]] if "email" in idx else r[0]).strip().lower()
        if r_email == email:
            stored_hash = (r[idx["senha_hash"]] if "senha_hash" in idx else r[1]).strip()
            if stored_hash and check_password_hash(stored_hash, senha):
                session["user_email"] = email
                return jsonify({"ok": True, "email": email})
            return jsonify({"ok": False, "error": "Senha inválida."}), 401

    return jsonify({"ok": False, "error": "Usuário não encontrado."}), 404


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.post("/api/reset_password")
def api_reset_password():
    """
    Reset simples (sem e-mail). Requer:
    { "email": "...", "nova_senha": "...", "confirmar": "..." }
    """
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    nova = data.get("nova_senha") or ""
    conf = data.get("confirmar") or ""

    if not email or not nova:
        return jsonify({"ok": False, "error": "Informe e-mail e nova senha."}), 400
    if nova != conf:
        return jsonify({"ok": False, "error": "As senhas não conferem."}), 400
    if len(nova) < 6:
        return jsonify({"ok": False, "error": "A senha deve ter pelo menos 6 caracteres."}), 400

    _, ws_users, _, _ = ensure_schema()
    rows = ws_users.get_all_values()
    if len(rows) <= 1:
        return jsonify({"ok": False, "error": "Nenhum usuário cadastrado."}), 404

    header = rows[0]
    try:
        email_col = header.index("email") + 1
        hash_col = header.index("senha_hash") + 1
    except ValueError:
        return jsonify({"ok": False, "error": "Cabeçalho da aba Usuarios inválido."}), 500

    for i, r in enumerate(rows[1:], start=2):  # linha real na planilha (começa em 2)
        if len(r) >= email_col and r[email_col - 1].strip().lower() == email:
            new_hash = generate_password_hash(nova)
            ws_users.update_cell(i, hash_col, new_hash)
            return jsonify({"ok": True})

    return jsonify({"ok": False, "error": "Usuário não encontrado."}), 404


# ---------------------------
# Finance APIs (exemplos)
# ---------------------------
@app.post("/api/lancamentos")
def api_add_lancamento():
    if not _require_login():
        return jsonify({"ok": False, "error": "Faça login para continuar."}), 401

    data = request.get_json(silent=True) or {}
    tipo = (data.get("tipo") or "").strip().upper()  # RECEITA / GASTO
    categoria = (data.get("categoria") or "").strip()
    descricao = (data.get("descricao") or "").strip()
    valor = data.get("valor")

    # Data opcional
    data_str = (data.get("data") or "").strip()
    if not data_str:
        data_str = date.today().isoformat()

    if tipo not in {"RECEITA", "GASTO"}:
        return jsonify({"ok": False, "error": "Tipo inválido (use RECEITA ou GASTO)."}), 400

    try:
        valor_num = float(str(valor).replace(".", "").replace(",", "."))
    except Exception:
        return jsonify({"ok": False, "error": "Valor inválido."}), 400

    _, _, ws_lanc, _ = ensure_schema()
    ws_lanc.append_row([
        session["user_email"],
        data_str,
        tipo,
        categoria,
        descricao,
        valor_num,
        _now_iso(),
    ])

    return jsonify({"ok": True})


@app.get("/api/dashboard")
def api_dashboard():
    if not _require_login():
        return jsonify({"ok": False, "error": "Faça login para continuar."}), 401

    # filtros: ?mes=2&ano=2026
    try:
        mes = int(request.args.get("mes") or date.today().month)
        ano = int(request.args.get("ano") or date.today().year)
    except Exception:
        return jsonify({"ok": False, "error": "Filtro de mês/ano inválido."}), 400

    _, _, ws_lanc, _ = ensure_schema()
    rows = ws_lanc.get_all_values()
    if len(rows) <= 1:
        return jsonify({"ok": True, "receitas": 0.0, "gastos": 0.0, "saldo": 0.0})

    header = rows[0]
    col = {name: header.index(name) for name in header}

    receitas = 0.0
    gastos = 0.0
    user = session["user_email"]

    for r in rows[1:]:
        if len(r) < len(header):
            continue
        if r[col["user_email"]].strip().lower() != user.lower():
            continue

        d = r[col["data"]].strip()
        try:
            y, m, _ = [int(x) for x in d.split("-")]
        except Exception:
            continue

        if y != ano or m != mes:
            continue

        tipo = r[col["tipo"]].strip().upper()
        try:
            v = float(str(r[col["valor"]]).replace(".", "").replace(",", "."))
        except Exception:
            continue

        if tipo == "RECEITA":
            receitas += v
        elif tipo == "GASTO":
            gastos += v

    saldo = receitas - gastos
    return jsonify({"ok": True, "receitas": receitas, "gastos": gastos, "saldo": saldo})


# ---------------------------
# Railway run
# ---------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
