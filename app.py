import os
import json
from datetime import datetime, date
from functools import lru_cache

from flask import Flask, request, jsonify, session, render_template
from werkzeug.security import generate_password_hash, check_password_hash

import gspread
from google.oauth2.service_account import Credentials


# ---------------------------
# Flask config
# ---------------------------
app = Flask(__name__)
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

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raw2 = raw.strip()
        if (raw2.startswith('"') and raw2.endswith('"')) or (raw2.startswith("'") and raw2.endswith("'")):
            raw2 = raw2[1:-1]
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
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=2000, cols=max(10, len(headers)))

    values = ws.get_all_values()
    if not values:
        ws.append_row(headers)
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
    return "user_email" in session


def _parse_money_to_float(v) -> float:
    # aceita "100,00" ou "100.00" ou "1.234,56"
    s = str(v).strip()
    if not s:
        raise ValueError("valor vazio")
    s = s.replace(" ", "")
    # se tem vírgula, assume decimal pt-BR e remove pontos de milhar
    if "," in s:
        s = s.replace(".", "")
        s = s.replace(",", ".")
    return float(s)


# ---------------------------
# Pages
# ---------------------------
@app.get("/")
def home():
    try:
        return render_template("index.html")
    except Exception:
        return "FinanceAI online ✅"


# ---------------------------
# Auth
# ---------------------------
@app.post("/api/register")
def api_register():
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

    _, ws_users, _, _ = ensure_schema()

    emails = ws_users.col_values(1)  # coluna A
    if email in [e.strip().lower() for e in emails[1:]]:
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
    try:
        email_i = header.index("email")
        hash_i = header.index("senha_hash")
    except ValueError:
        return jsonify({"ok": False, "error": "Cabeçalho da aba Usuarios inválido."}), 500

    for r in rows[1:]:
        if len(r) <= max(email_i, hash_i):
            continue
        if r[email_i].strip().lower() == email:
            stored_hash = r[hash_i].strip()
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

    for i, r in enumerate(rows[1:], start=2):
        if len(r) >= email_col and r[email_col - 1].strip().lower() == email:
            ws_users.update_cell(i, hash_col, generate_password_hash(nova))
            return jsonify({"ok": True})

    return jsonify({"ok": False, "error": "Usuário não encontrado."}), 404


# ---------------------------
# Lancamentos (CRUD)
# ---------------------------
@app.post("/api/lancamentos")
def api_add_lancamento():
    if not _require_login():
        return jsonify({"ok": False, "error": "Faça login para continuar."}), 401

    data = request.get_json(silent=True) or {}
    tipo = (data.get("tipo") or "").strip().upper()
    categoria = (data.get("categoria") or "").strip()
    descricao = (data.get("descricao") or "").strip()
    valor = data.get("valor")
    data_str = (data.get("data") or "").strip() or date.today().isoformat()

    if tipo not in {"RECEITA", "GASTO"}:
        return jsonify({"ok": False, "error": "Tipo inválido (use RECEITA ou GASTO)."}), 400

    try:
        valor_num = _parse_money_to_float(valor)
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


@app.get("/api/lancamentos")
def api_list_lancamentos():
    if not _require_login():
        return jsonify({"ok": False, "error": "Faça login para continuar."}), 401

    try:
        limit = int(request.args.get("limit") or 30)
        limit = max(1, min(limit, 200))
    except Exception:
        limit = 30

    _, _, ws_lanc, _ = ensure_schema()
    rows = ws_lanc.get_all_values()
    if len(rows) <= 1:
        return jsonify({"ok": True, "items": []})

    header = rows[0]
    col = {name: header.index(name) for name in header}
    user = session["user_email"].lower()

    items = []
    for sheet_row_number in range(len(rows), 1, -1):  # de baixo pra cima
        r = rows[sheet_row_number - 1]
        if len(r) < len(header):
            continue
        if r[col["user_email"]].strip().lower() != user:
            continue

        items.append({
            "row": sheet_row_number,  # linha real na planilha
            "data": r[col["data"]].strip(),
            "tipo": r[col["tipo"]].strip(),
            "categoria": r[col["categoria"]].strip(),
            "descricao": r[col["descricao"]].strip(),
            "valor": r[col["valor"]].strip(),
        })
        if len(items) >= limit:
            break

    return jsonify({"ok": True, "items": items})


@app.put("/api/lancamentos/<int:row>")
def api_update_lancamento(row: int):
    if not _require_login():
        return jsonify({"ok": False, "error": "Faça login para continuar."}), 401
    if row < 2:
        return jsonify({"ok": False, "error": "Linha inválida."}), 400

    data = request.get_json(silent=True) or {}
    new_data = (data.get("data") or "").strip()
    new_tipo = (data.get("tipo") or "").strip().upper()
    new_categoria = (data.get("categoria") or "").strip()
    new_descricao = (data.get("descricao") or "").strip()
    new_valor = data.get("valor")

    if not new_data:
        return jsonify({"ok": False, "error": "Informe a data (YYYY-MM-DD)."}), 400
    if new_tipo not in {"RECEITA", "GASTO"}:
        return jsonify({"ok": False, "error": "Tipo inválido (use RECEITA ou GASTO)."}), 400
    try:
        valor_num = _parse_money_to_float(new_valor)
    except Exception:
        return jsonify({"ok": False, "error": "Valor inválido."}), 400

    _, _, ws_lanc, _ = ensure_schema()

    try:
        header = ws_lanc.row_values(1)
        col = {name: header.index(name) + 1 for name in header}  # 1-based

        user_email_col = col.get("user_email")
        if not user_email_col:
            return jsonify({"ok": False, "error": "Cabeçalho da aba Lancamentos inválido."}), 500

        owner = (ws_lanc.cell(row, user_email_col).value or "").strip().lower()
        if owner != session["user_email"].lower():
            return jsonify({"ok": False, "error": "Você não pode editar este lançamento."}), 403

        # Atualiza somente colunas de edição (não mexe no criado_em)
        ws_lanc.update_cell(row, col["data"], new_data)
        ws_lanc.update_cell(row, col["tipo"], new_tipo)
        ws_lanc.update_cell(row, col["categoria"], new_categoria)
        ws_lanc.update_cell(row, col["descricao"], new_descricao)
        ws_lanc.update_cell(row, col["valor"], valor_num)

        return jsonify({"ok": True})

    except gspread.exceptions.APIError as e:
        return jsonify({"ok": False, "error": f"Erro ao editar: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Erro ao editar: {str(e)}"}), 500


@app.delete("/api/lancamentos/<int:row>")
def api_delete_lancamento(row: int):
    if not _require_login():
        return jsonify({"ok": False, "error": "Faça login para continuar."}), 401
    if row < 2:
        return jsonify({"ok": False, "error": "Linha inválida."}), 400

    _, _, ws_lanc, _ = ensure_schema()

    try:
        header = ws_lanc.row_values(1)
        col = {name: header.index(name) + 1 for name in header}  # 1-based

        user_email_col = col.get("user_email")
        if not user_email_col:
            return jsonify({"ok": False, "error": "Cabeçalho da aba Lancamentos inválido."}), 500

        owner = (ws_lanc.cell(row, user_email_col).value or "").strip().lower()
        if owner != session["user_email"].lower():
            return jsonify({"ok": False, "error": "Você não pode apagar este lançamento."}), 403

        ws_lanc.delete_rows(row)
        return jsonify({"ok": True})

    except gspread.exceptions.APIError as e:
        return jsonify({"ok": False, "error": f"Erro ao apagar: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Erro ao apagar: {str(e)}"}), 500


# ---------------------------
# Dashboard
# ---------------------------
@app.get("/api/dashboard")
def api_dashboard():
    if not _require_login():
        return jsonify({"ok": False, "error": "Faça login para continuar."}), 401

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
    user = session["user_email"].lower()

    for r in rows[1:]:
        if len(r) < len(header):
            continue
        if r[col["user_email"]].strip().lower() != user:
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
