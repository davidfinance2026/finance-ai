import os
import json
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

# Escopos necessários p/ ler e escrever em Google Sheets
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
    """
    Aceita 'dd/mm/aaaa'. Se vazio, usa hoje.
    """
    if not s:
        return datetime.now().date()

    s = s.strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    if not m:
        # tenta ISO 'yyyy-mm-dd'
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            raise ValueError("Data inválida. Use dd/mm/aaaa.")

    dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return date(yyyy, mm, dd)


def _to_float(v: Any) -> float:
    """
    Converte valor vindo da planilha (string) para float.
    Aceita: '120', '120.5', '120,50', 'R$ 120,50'
    """
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return 0.0

    s = s.replace("R$", "").strip()
    s = s.replace(".", "").replace(",", ".")  # pt-BR para float
    # remove qualquer coisa que não seja número, ponto ou sinal
    s = re.sub(r"[^0-9\.\-]", "", s)
    try:
        return float(s)
    except Exception:
        return 0.0


def _get_ws():
    """
    Abre a worksheet (aba) uma vez e reutiliza.
    """
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
    """
    Garante que a primeira linha tenha os cabeçalhos corretos.
    """
    expected = ["Data", "Tipo", "Categoria", "Descrição", "Valor"]
    first = ws.row_values(1)

    if [c.strip() for c in first[:5]] != expected:
        # se estiver vazio, cria cabeçalho
        if len(first) == 0:
            ws.append_row(expected)
        else:
            # tenta corrigir só se a planilha estiver "quase"
            ws.update("A1:E1", [expected])


def _fetch_rows(ws) -> List[Dict[str, Any]]:
    """
    Lê todas as linhas (exceto cabeçalho) e devolve lista de dicts.
    """
    values = ws.get_all_values()  # inclui cabeçalho
    if not values or len(values) < 2:
        return []

    header = values[0]
    rows = values[1:]

    # garante colunas mínimas
    # Data, Tipo, Categoria, Descrição, Valor
    out = []
    for r in rows:
        # pad para 5 colunas
        r = (r + ["", "", "", "", ""])[:5]
        item = {
            "Data": r[0].strip(),
            "Tipo": r[1].strip(),
            "Categoria": r[2].strip(),
            "Descrição": r[3].strip(),
            "Valor": _to_float(r[4]),
        }
        # ignora linhas totalmente vazias
        if any([item["Data"], item["Tipo"], item["Categoria"], item["Descrição"], item["Valor"]]):
            out.append(item)
    return out


def _month_key(d: date) -> Tuple[int, int]:
    return (d.year, d.month)


def _group_category_sum(items: List[Dict[str, Any]]) -> Tuple[List[str], List[float]]:
    """
    Soma por categoria (case-insensitive), mantendo o primeiro nome visto.
    Ordena por maior valor.
    """
    sums: Dict[str, float] = {}
    display: Dict[str, str] = {}

    for it in items:
        cat_raw = (it.get("Categoria") or "").strip()
        if not cat_raw:
            cat_raw = "Sem categoria"
        key = cat_raw.lower()

        if key not in display:
            display[key] = cat_raw
        sums[key] = sums.get(key, 0.0) + float(it.get("Valor") or 0.0)

    # ordena desc
    pairs = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)

    labels = [display[k] for k, _ in pairs]
    values = [round(v, 2) for _, v in pairs]
    return labels, values


# =========================
# ROUTES
# =========================
@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/")
def home():
    # precisa existir: templates/index.html
    return render_template("index.html")


@app.post("/lancar")
def lancar():
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return _fail("JSON inválido.")

    tipo = (data.get("tipo") or "").strip()
    categoria = (data.get("categoria") or "").strip()
    descricao = (data.get("descricao") or "").strip()
    valor = data.get("valor")
    data_str = (data.get("data") or "").strip()  # dd/mm/aaaa

    if tipo not in ["Gasto", "Receita"]:
        return _fail("Campo 'tipo' deve ser 'Gasto' ou 'Receita'.")
    if not categoria:
        return _fail("Informe a categoria.")
    if not descricao:
        return _fail("Informe a descrição.")

    try:
        valor_f = float(valor)
    except Exception:
        valor_f = _to_float(valor)
    if valor_f <= 0:
        return _fail("Valor deve ser maior que zero.")

    try:
        d = _parse_ddmmyyyy(data_str)
        data_fmt = d.strftime("%d/%m/%Y")
    except Exception as e:
        return _fail(str(e))

    try:
        ws = _get_ws()
        _ensure_header(ws)

        # append: Data, Tipo, Categoria, Descrição, Valor
        ws.append_row([data_fmt, tipo, categoria, descricao, f"{valor_f:.2f}"])
        return jsonify({"ok": True})
    except Exception as e:
        return _fail(f"Erro ao salvar na planilha: {e}", 500)


@app.get("/ultimos")
def ultimos():
    try:
        ws = _get_ws()
        _ensure_header(ws)
        rows = _fetch_rows(ws)

        # últimos 10 (pela ordem em que estão na planilha)
        last = rows[-10:]
        # devolve como lista (front já sabe lidar)
        # mantém 'Valor' numérico
        return jsonify(last)
    except Exception as e:
        return _fail(f"Erro ao ler planilha: {e}", 500)


@app.get("/resumo")
def resumo():
    """
    Retorna:
      entradas, saidas, saldo,
      dias, serie_receita, serie_gasto,
      pizza_gastos_labels, pizza_gastos_values,
      pizza_receitas_labels, pizza_receitas_values
    """
    try:
        ws = _get_ws()
        _ensure_header(ws)
        rows = _fetch_rows(ws)

        hoje = datetime.now().date()
        y, m = hoje.year, hoje.month
        days_in_month = calendar.monthrange(y, m)[1]

        # Filtra só lançamentos do mês atual
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

        # totais
        entradas = sum(i["Valor"] for i in month_items if (i.get("Tipo") or "").lower().startswith("rece"))
        saidas = sum(i["Valor"] for i in month_items if (i.get("Tipo") or "").lower().startswith("gas"))
        saldo = entradas - saidas

        # séries por dia (1..days_in_month)
        serie_receita = [0.0] * days_in_month
        serie_gasto = [0.0] * days_in_month

        for it in month_items:
            d: date = it["_date"]
            idx = d.day - 1
            if idx < 0 or idx >= days_in_month:
                continue
            tipo = (it.get("Tipo") or "").lower()
            if tipo.startswith("rece"):
                serie_receita[idx] += float(it["Valor"])
            elif tipo.startswith("gas"):
                serie_gasto[idx] += float(it["Valor"])

        serie_receita = [round(v, 2) for v in serie_receita]
        serie_gasto = [round(v, 2) for v in serie_gasto]
        dias = list(range(1, days_in_month + 1))

        # pizzas (por categoria) separadas
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

            # pizzas
            "pizza_gastos_labels": pizza_gastos_labels,
            "pizza_gastos_values": pizza_gastos_values,
            "pizza_receitas_labels": pizza_receitas_labels,
            "pizza_receitas_values": pizza_receitas_values,
        })

    except Exception as e:
        return _fail(f"Erro no /resumo: {e}", 500)


# Importante: no Render você roda via gunicorn (Procfile),
# então NÃO precisa app.run() aqui.
# Se quiser testar local:
# if __name__ == "__main__":
#     app.run(host="0.0.0.0", port=5000, debug=True)
