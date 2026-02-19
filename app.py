import os
import re
import calendar
from datetime import datetime, date
from typing import Any, Dict, List, Tuple

from flask import Flask, jsonify, render_template, request
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# =========================
# CONFIG
# =========================
SHEET_ID = os.environ.get("SHEET_ID", "").strip()
SHEET_TAB = os.environ.get("SHEET_TAB", "Lancamentos").strip()

# Render Secret File: /etc/secrets/google_creds.json
CREDS_PATH = os.environ.get("GOOGLE_CREDS_PATH", "/etc/secrets/google_creds.json")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_client = None
_ws = None


# =========================
# HELPERS
# =========================
def _fail(msg: str, code: int = 400):
    return jsonify({"ok": False, "msg": msg}), code


def _parse_ddmmyyyy(s: str) -> date:
    if not s:
        return datetime.now().date()
    s = s.strip()

    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    if m:
        dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return date(yyyy, mm, dd)

    # tenta ISO yyyy-mm-dd
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        raise ValueError("Data inválida. Use dd/mm/aaaa.")


def _to_float(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)

    s = str(v).strip()
    if not s:
        return 0.0

    s = s.replace("R$", "").strip()
    s = s.replace(".", "").replace(",", ".")
    s = re.sub(r"[^0-9\.\-]", "", s)
    try:
        return float(s)
    except Exception:
        return 0.0


def _get_ws():
    global _client, _ws

    if not SHEET_ID:
        raise RuntimeError("SHEET_ID não definido nas variáveis do Render.")
    if not SHEET_TAB:
        raise RuntimeError("SHEET_TAB não definido nas variáveis do Render.")

    if _ws is not None:
        return _ws

    if _client is None:
        if not os.path.exists(CREDS_PATH):
            raise RuntimeError(f"Arquivo de credenciais não encontrado em {CREDS_PATH}")
        creds = Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)
        _client = gspread.authorize(creds)

    sh = _client.open_by_key(SHEET_ID)
    _ws = sh.worksheet(SHEET_TAB)
    return _ws


def _ensure_header(ws):
    expected = ["Data", "Tipo", "Categoria", "Descrição", "Valor"]
    first = ws.row_values(1)

    if len(first) == 0:
        ws.append_row(expected)
        return

    first5 = [c.strip() for c in (first + [""] * 5)[:5]]
    if first5 != expected:
        ws.update("A1:E1", [expected])


def _fetch_rows_with_rownum(ws) -> List[Dict[str, Any]]:
    """
    Retorna lista de dicts com campo _row (linha real na planilha).
    Linha 1 é cabeçalho, então dados começam em 2.
    """
    values = ws.get_all_values()
    if not values or len(values) < 2:
        return []

    rows = values[1:]
    out = []
    for i, r in enumerate(rows, start=2):  # start=2 => primeira linha de dados é 2
        r = (r + ["", "", "", "", ""])[:5]
        item = {
            "_row": i,
            "Data": r[0].strip(),
            "Tipo": r[1].strip(),
            "Categoria": r[2].strip(),
            "Descrição": r[3].strip(),
            "Valor": _to_float(r[4]),
        }
        if any([item["Data"], item["Tipo"], item["Categoria"], item["Descrição"], item["Valor"]]):
            out.append(item)
    return out


def _month_key(d: date) -> Tuple[int, int]:
    return (d.year, d.month)


def _group_category_sum(items: List[Dict[str, Any]]) -> Tuple[List[str], List[float]]:
    sums: Dict[str, float] = {}
    display: Dict[str, str] = {}

    for it in items:
        cat_raw = (it.get("Categoria") or "").strip() or "Sem categoria"
        key = cat_raw.lower()

        if key not in display:
            display[key] = cat_raw
        sums[key] = sums.get(key, 0.0) + float(it.get("Valor") or 0.0)

    pairs = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)
    labels = [display[k] for k, _ in pairs]
    values = [round(v, 2) for _, v in pairs]
    return labels, values


def _validate_payload(data: Dict[str, Any]) -> Tuple[str, str, str, float, str]:
    tipo = (data.get("tipo") or "").strip()
    categoria = (data.get("categoria") or "").strip()
    descricao = (data.get("descricao") or "").strip()
    valor = data.get("valor")
    data_str = (data.get("data") or "").strip()

    if tipo not in ["Gasto", "Receita"]:
        raise ValueError("Campo 'tipo' deve ser 'Gasto' ou 'Receita'.")
    if not categoria:
        raise ValueError("Informe a categoria.")
    if not descricao:
        raise ValueError("Informe a descrição.")

    try:
        valor_f = float(valor)
    except Exception:
        valor_f = _to_float(valor)
    if valor_f <= 0:
        raise ValueError("Valor deve ser maior que zero.")

    d = _parse_ddmmyyyy(data_str)
    data_fmt = d.strftime("%d/%m/%Y")

    return tipo, categoria, descricao, valor_f, data_fmt


# =========================
# ROUTES
# =========================
@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/")
def home():
    return render_template("index.html")


@app.post("/lancar")
def lancar():
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return _fail("JSON inválido.")

    try:
        tipo, categoria, descricao, valor_f, data_fmt = _validate_payload(data)
    except Exception as e:
        return _fail(str(e))

    try:
        ws = _get_ws()
        _ensure_header(ws)
        ws.append_row([data_fmt, tipo, categoria, descricao, f"{valor_f:.2f}"])
        return jsonify({"ok": True})
    except Exception as e:
        return _fail(f"Erro ao salvar na planilha: {e}", 500)


@app.get("/ultimos")
def ultimos():
    """
    Retorna os últimos 10 com _row (linha na planilha) para permitir editar/excluir.
    """
    try:
        ws = _get_ws()
        _ensure_header(ws)
        rows = _fetch_rows_with_rownum(ws)
        return jsonify(rows[-10:])
    except Exception as e:
        return _fail(f"Erro ao ler planilha: {e}", 500)


@app.patch("/lancamento/<int:row>")
def editar(row: int):
    """
    Edita a linha 'row' na planilha (row >= 2).
    """
    if row < 2:
        return _fail("Linha inválida.", 400)

    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return _fail("JSON inválido.")

    try:
        tipo, categoria, descricao, valor_f, data_fmt = _validate_payload(data)
    except Exception as e:
        return _fail(str(e))

    try:
        ws = _get_ws()
        _ensure_header(ws)

        # atualiza A..E da linha
        ws.update(f"A{row}:E{row}", [[data_fmt, tipo, categoria, descricao, f"{valor_f:.2f}"]])
        return jsonify({"ok": True})
    except Exception as e:
        return _fail(f"Erro ao editar: {e}", 500)


@app.delete("/lancamento/<int:row>")
def excluir(row: int):
    """
    Exclui a linha 'row' na planilha (row >= 2).
    """
    if row < 2:
        return _fail("Linha inválida.", 400)

    try:
        ws = _get_ws()
        _ensure_header(ws)
        ws.delete_rows(row)
        return jsonify({"ok": True})
    except Exception as e:
        return _fail(f"Erro ao excluir: {e}", 500)


@app.get("/resumo")
def resumo():
    try:
        ws = _get_ws()
        _ensure_header(ws)
        rows = _fetch_rows_with_rownum(ws)

        hoje = datetime.now().date()
        y, m = hoje.year, hoje.month
        days_in_month = calendar.monthrange(y, m)[1]

        month_items = []
        for it in rows:
            try:
                d = _parse_ddmmyyyy(it.get("Data", ""))
            except Exception:
                continue
            if _month_key(d) == (y, m):
                it2 = dict(it)
                it2["_date"] = d
                month_items.append(it2)

        entradas = sum(i["Valor"] for i in month_items if (i.get("Tipo") or "").lower().startswith("rece"))
        saidas = sum(i["Valor"] for i in month_items if (i.get("Tipo") or "").lower().startswith("gas"))
        saldo = entradas - saidas

        serie_receita = [0.0] * days_in_month
        serie_gasto = [0.0] * days_in_month

        for it in month_items:
            d: date = it["_date"]
            idx = d.day - 1
            if 0 <= idx < days_in_month:
                tipo = (it.get("Tipo") or "").lower()
                if tipo.startswith("rece"):
                    serie_receita[idx] += float(it["Valor"])
                elif tipo.startswith("gas"):
                    serie_gasto[idx] += float(it["Valor"])

        serie_receita = [round(v, 2) for v in serie_receita]
        serie_gasto = [round(v, 2) for v in serie_gasto]
        dias = list(range(1, days_in_month + 1))

        gastos_items = [i for i in month_items if (i.get("Tipo") or "").lower().startswith("gas")]
        receitas_items = [i for i in month_items if (i.get("Tipo") or "").lower().startswith("rece")]

        pizza_gastos_labels, pizza_gastos_values = _group_category_sum(gastos_items)
        pizza_receitas_labels, pizza_receitas_values = _group_category_sum(receitas_items)

        return jsonify({
            "entradas": round(entradas, 2),
            "saidas": round(saidas, 2),
            "saldo": round(saldo, 2),
            "dias": dias,
            "serie_receita": serie_receita,
            "serie_gasto": serie_gasto,
            "pizza_gastos_labels": pizza_gastos_labels,
            "pizza_gastos_values": pizza_gastos_values,
            "pizza_receitas_labels": pizza_receitas_labels,
            "pizza_receitas_values": pizza_receitas_values,
        })
    except Exception as e:
        return _fail(f"Erro no /resumo: {e}", 500)
