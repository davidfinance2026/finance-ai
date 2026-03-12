# -*- coding: utf-8 -*-
import re
from datetime import datetime

from utils_core import norm_word, tokenize, parse_brl_value
from utils_auth import normalize_email
from utils_workflows import parse_kv_assignments

CONNECT_ALIASES = ("conectar", "vincular", "linkar", "associar", "registrar", "conexao", "conexão")
NEGATIONS = {"nao", "não", "nunca", "jamais"}

VALUE_RE = re.compile(r"([+\-])?\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:[.,]\d{1,2})?)")

INCOME_HINTS = {
    "recebi", "recebido", "recebida", "entrou", "entrada", "caiu",
    "deposito", "depósito", "salario", "salário", "venda", "vendido",
    "comissao", "comissão", "bonus", "bônus", "reembolso", "ganhei", "renda", "receita",
    "pixrecebido", "pix_recebido",
}
EXPENSE_HINTS = {
    "paguei", "pago", "pagar", "comprei", "compra", "gastei", "gasto", "despesa",
    "saida", "saída", "debito", "débito", "boleto", "conta", "fatura", "cartao", "cartão",
}

CMD_HELP_RE = re.compile(r"^\s*(ajuda|\?|help)\s*$", re.IGNORECASE)
CMD_ULTIMOS_RE = re.compile(r"^\s*ultimos\s*$", re.IGNORECASE)
CMD_APAGAR_RE = re.compile(r"^\s*apagar\s+(\d+)\s*$", re.IGNORECASE)
CMD_CORRIGIR_ULTIMA_RE = re.compile(r"^\s*corrigir\s+ultima\s+(.+)$", re.IGNORECASE)
CMD_EDITAR_RE = re.compile(r"^\s*editar\s+(\d+)\s+(.+)$", re.IGNORECASE)
CMD_DESFAZER_RE = re.compile(r"^\s*desfazer\s*$", re.IGNORECASE)

CMD_RESUMO_RE = re.compile(r"^\s*resumo\s+(hoje|dia|semana|mes|m[eê]s)\s*$", re.IGNORECASE)
CMD_SALDO_MES_RE = re.compile(r"^\s*saldo\s+m[eê]s\s*$", re.IGNORECASE)
CMD_ANALISE_RE = re.compile(r"^\s*(analise|an[aá]lise|insights)\s*(hoje|semana|mes|m[eê]s)?\s*$", re.IGNORECASE)
CMD_PROJECAO_RE = re.compile(r"^\s*(projecao|projeção)\s*$", re.IGNORECASE)
CMD_ALERTAS_RE = re.compile(r"^\s*alertas?\s*$", re.IGNORECASE)

CAT_SET_RE = re.compile(r"^\s*categoria\s+(.+?)\s*=\s*(.+?)\s*$", re.IGNORECASE)
CAT_DEL_RE = re.compile(r"^\s*remover\s+categoria\s+(.+?)\s*$", re.IGNORECASE)
CAT_LIST_RE = re.compile(r"^\s*categorias\s*$", re.IGNORECASE)

REC_ADD_RE = re.compile(r"^\s*recorrente\s+(diario|di[aá]rio|semanal|mensal)\s+(.+)$", re.IGNORECASE)
REC_LIST_RE = re.compile(r"^\s*recorrentes\s*$", re.IGNORECASE)
REC_DEL_RE = re.compile(r"^\s*remover\s+recorrente\s+(\d+)\s*$", re.IGNORECASE)
REC_RUN_RE = re.compile(r"^\s*(gerar|rodar)\s+recorrentes\s*$", re.IGNORECASE)

WEEKDAY_MAP = {
    "seg": 0, "segunda": 0,
    "ter": 1, "terça": 1, "terca": 1,
    "qua": 2, "quarta": 2,
    "qui": 3, "quinta": 3,
    "sex": 4, "sexta": 4,
    "sab": 5, "sábado": 5, "sabado": 5,
    "dom": 6, "domingo": 6,
}


def detect_tipo_with_score(sign: str, before_tokens: list[str], after_tokens: list[str]):
    if sign == "+":
        return "RECEITA", "high"
    if sign == "-":
        return "GASTO", "high"

    bset = set(before_tokens)
    aset = set(after_tokens)

    income_set = {norm_word(x) for x in INCOME_HINTS}
    expense_set = {norm_word(x) for x in EXPENSE_HINTS}

    b_income = len(bset & income_set)
    b_exp = len(bset & expense_set)
    a_income = len(aset & income_set)
    a_exp = len(aset & expense_set)

    score_income = (b_income * 3) + a_income
    score_exp = (b_exp * 3) + a_exp

    has_neg = any(t in {norm_word(n) for n in NEGATIONS} for n in NEGATIONS for t in before_tokens[:2])
    if has_neg and score_income > 0 and score_exp == 0:
        score_income = 0

    if score_income == 0 and score_exp == 0:
        return "GASTO", "low"
    if score_income == score_exp:
        return ("RECEITA" if score_income > 0 else "GASTO"), "low"
    if score_income > score_exp:
        return "RECEITA", ("high" if (score_income - score_exp) >= 2 else "low")
    return "GASTO", ("high" if (score_exp - score_income) >= 2 else "low")


def wa_help_text():
    return (
        "✅ Comandos disponíveis:\n\n"
        "🔗 Conectar:\n"
        "• conectar seuemail@dominio.com\n\n"
        "🧾 Lançar:\n"
        "• recebi 1200 salario\n"
        "• paguei 32,90 mercado\n"
        "• + 35,90 venda camiseta\n"
        "• - 18,00 uber\n\n"
        "📊 Inteligência Finance AI:\n"
        "• projeção\n"
        "• alertas\n"
        "• analise\n"
        "• analise semana\n"
        "• analise mês\n"
        "• quanto gastei esse mês\n"
        "• quanto tenho investido\n"
        "• qual meu saldo previsto\n\n"
        "🧠 Se houver dúvida, eu pergunto: RECEITA ou GASTO.\n"
        "Responda apenas: receita  (ou)  gasto\n\n"
        "✏️ Corrigir aqui:\n"
        "• ultimos\n"
        "• apagar 123\n"
        "• editar 123 valor=35,90 categoria=Alimentação data=2026-03-01 descricao=\"algo\" tipo=receita\n"
        "• corrigir ultima categoria=Transporte\n\n"
        "↩️ Desfazer (janela de 5 min):\n"
        "• desfazer\n\n"
        "📊 Resumos:\n"
        "• resumo hoje\n"
        "• resumo semana\n"
        "• resumo mês\n"
        "• saldo mês\n\n"
        "🔁 Recorrentes:\n"
        "• recorrente mensal 5 1200 aluguel\n"
        "• recorrente semanal seg 50 academia\n"
        "• recorrente diário 10 cafe\n"
        "• recorrentes\n"
        "• remover recorrente 7\n"
        "• rodar recorrentes\n\n"
        "🏷️ Ensinar categorias:\n"
        "• categorias\n"
        "• categoria ifood = Alimentação\n"
        "• remover categoria ifood\n"
    )


def parse_wa_text(msg_text: str):
    t = (msg_text or "").strip()
    if not t:
        return {"cmd": "NONE"}

    if CMD_HELP_RE.match(t):
        return {"cmd": "HELP"}

    if CMD_DESFAZER_RE.match(t):
        return {"cmd": "DESFAZER"}

    if CMD_PROJECAO_RE.match(t):
        return {"cmd": "PROJECAO"}

    if CMD_ALERTAS_RE.match(t):
        return {"cmd": "ALERTAS"}

    m = CMD_RESUMO_RE.match(t)
    if m:
        return {"cmd": "RESUMO", "kind": m.group(1)}

    if CMD_SALDO_MES_RE.match(t):
        return {"cmd": "SALDO_MES"}

    m = CMD_ANALISE_RE.match(t)
    if m:
        return {"cmd": "ANALISE", "kind": m.group(2)}

    mset = CAT_SET_RE.match(t)
    if mset:
        key = mset.group(1).strip()
        cat = mset.group(2).strip()
        if not key or not cat:
            return {"cmd": "CAT_HELP"}
        return {"cmd": "CAT_SET", "key": key, "categoria": cat}

    mdel = CAT_DEL_RE.match(t)
    if mdel:
        key = mdel.group(1).strip()
        if not key:
            return {"cmd": "CAT_HELP"}
        return {"cmd": "CAT_DEL", "key": key}

    if CAT_LIST_RE.match(t):
        return {"cmd": "CAT_LIST"}

    m = REC_ADD_RE.match(t)
    if m:
        return {"cmd": "REC_ADD", "freq": m.group(1), "rest": m.group(2)}

    if REC_LIST_RE.match(t):
        return {"cmd": "REC_LIST"}

    m = REC_DEL_RE.match(t)
    if m:
        return {"cmd": "REC_DEL", "id": int(m.group(1))}

    if REC_RUN_RE.match(t):
        return {"cmd": "REC_RUN"}

    if CMD_ULTIMOS_RE.match(t):
        return {"cmd": "ULTIMOS"}

    m = CMD_APAGAR_RE.match(t)
    if m:
        return {"cmd": "APAGAR", "id": int(m.group(1))}

    m = CMD_CORRIGIR_ULTIMA_RE.match(t)
    if m:
        return {"cmd": "CORRIGIR_ULTIMA", "fields": parse_kv_assignments(m.group(1))}

    m = CMD_EDITAR_RE.match(t)
    if m:
        return {"cmd": "EDITAR", "id": int(m.group(1)), "fields": parse_kv_assignments(m.group(2))}

    low_simple = norm_word(t)
    if low_simple in ("receita", "gasto"):
        return {"cmd": "CONFIRM_TIPO", "tipo": "RECEITA" if low_simple == "receita" else "GASTO"}

    low = norm_word(t)
    low = re.sub(r"\s+", " ", low).strip()
    for alias in CONNECT_ALIASES:
        if low.startswith(norm_word(alias) + " "):
            email = t.split(" ", 1)[1].strip()
            return {"cmd": "CONNECT", "email": normalize_email(email)}

    m = VALUE_RE.search(low)
    if not m:
        return {"cmd": "NONE"}

    sign = m.group(1) or ""
    valor_raw = m.group(2)
    try:
        valor = parse_brl_value(valor_raw)
    except Exception:
        return {"cmd": "NONE"}

    before = (low[:m.start()] or "").strip()
    after = (low[m.end():] or "").strip(" -–—")

    before_tokens = tokenize(before)
    after_tokens = tokenize(after)

    tipo, confidence = detect_tipo_with_score(sign, before_tokens, after_tokens)

    categoria_fallback = "Outros"
    descricao = ""
    if after:
        parts = after.split(" ", 1)
        categoria_fallback = (parts[0] or "Outros").strip().title()
        descricao = parts[1].strip() if len(parts) > 1 else ""

    return {
        "cmd": "TX",
        "tipo": tipo,
        "tipo_confidence": confidence,
        "valor": valor,
        "categoria_fallback": categoria_fallback,
        "descricao": descricao,
        "data": datetime.utcnow().date(),
        "raw_text": t,
    }
