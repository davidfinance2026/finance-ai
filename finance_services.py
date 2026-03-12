# -*- coding: utf-8 -*-
import calendar
from datetime import datetime, date, timedelta
from decimal import Decimal

import requests

from utils_core import month_bounds, fmt_brl, norm_word, tokenize, period_range

WEEKDAY_MAP = {
    "segunda": 0,
    "terça": 1,
    "terca": 1,
    "quarta": 2,
    "quinta": 3,
    "sexta": 4,
    "sábado": 5,
    "sabado": 5,
    "domingo": 6,
}

_CFG = {
    "Transaction": None,
    "Investment": None,
    "RecurringRule": None,
    "CategoryRule": None,
    "openai_chat_model": "gpt-4.1-mini",
    "openai_available_func": None,
    "openai_headers_func": None,
}


def init_finance_services(
    *,
    Transaction,
    Investment,
    RecurringRule,
    CategoryRule,
    openai_chat_model,
    openai_available_func,
    openai_headers_func,
):
    _CFG.update({
        "Transaction": Transaction,
        "Investment": Investment,
        "RecurringRule": RecurringRule,
        "CategoryRule": CategoryRule,
        "openai_chat_model": openai_chat_model or "gpt-4.1-mini",
        "openai_available_func": openai_available_func,
        "openai_headers_func": openai_headers_func,
    })


def _models():
    return (
        _CFG["Transaction"],
        _CFG["Investment"],
        _CFG["RecurringRule"],
        _CFG["CategoryRule"],
    )


def guess_category_from_text(user_id: int, full_text: str) -> str | None:
    _, _, _, CategoryRule = _models()
    tokens = set(tokenize(full_text))

    try:
        rules = (
            CategoryRule.query
            .filter(CategoryRule.user_id == user_id)
            .order_by(CategoryRule.priority.desc(), CategoryRule.id.desc())
            .all()
        )
        for r in rules:
            key = norm_word(r.pattern)
            if not key:
                continue
            if key in tokens or any(key in t for t in tokens):
                return (r.categoria or "").strip().title() or None
    except Exception:
        pass

    default_category_keywords = [
        ("Alimentação", {"ifood", "i-food", "restaurante", "lanchonete", "pizza", "burguer", "hamburguer", "lanche", "mercado", "padaria", "cafe", "café"}),
        ("Transporte", {"uber", "99", "taxi", "táxi", "onibus", "ônibus", "metro", "metrô", "gasolina", "etanol", "combustivel", "combustível", "estacionamento"}),
        ("Moradia", {"aluguel", "condominio", "condomínio", "iptu", "prestacao", "prestação", "financiamento", "luz", "energia", "agua", "água", "internet"}),
        ("Saúde", {"farmacia", "farmácia", "remedio", "remédio", "medico", "médico", "consulta", "exame", "dentista"}),
        ("Educação", {"curso", "faculdade", "escola", "mensalidade", "livro"}),
        ("Lazer", {"cinema", "show", "bar", "viagem", "hotel"}),
        ("Impostos", {"imposto", "taxa", "multa"}),
        ("Transferências", {"pix", "ted", "doc", "transferencia", "transferência"}),
    ]

    for cat, keys in default_category_keywords:
        nkeys = {norm_word(k) for k in keys}
        if tokens & nkeys:
            return cat

    return None


def sum_period(user_id: int, start: date, end: date):
    Transaction, _, _, _ = _models()
    q = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
        .filter(Transaction.data >= start)
        .filter(Transaction.data < end)
        .all()
    )

    receitas = Decimal("0")
    gastos = Decimal("0")
    for t in q:
        v = Decimal(t.valor or 0)
        if (t.tipo or "").upper() == "RECEITA":
            receitas += v
        else:
            gastos += v

    return receitas, gastos, (receitas - gastos), q


def calc_projection(user_id: int, ref_date: date | None = None):
    Transaction, _, RecurringRule, _ = _models()
    today = ref_date or datetime.utcnow().date()
    start, end = month_bounds(today.year, today.month)

    rows = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
        .filter(Transaction.data >= start)
        .filter(Transaction.data < end)
        .all()
    )

    receitas = Decimal("0")
    gastos = Decimal("0")
    gastos_variaveis = Decimal("0")

    for t in rows:
        v = Decimal(t.valor or 0)
        if (t.tipo or "").upper() == "RECEITA":
            receitas += v
        else:
            gastos += v
            if (t.origem or "").upper() != "REC":
                gastos_variaveis += v

    saldo_atual = receitas - gastos

    future_receitas_rec = Decimal("0")
    future_gastos_rec = Decimal("0")

    recurring_rules = (
        RecurringRule.query
        .filter(RecurringRule.user_id == user_id, RecurringRule.is_active.is_(True))
        .all()
    )

    for r in recurring_rules:
        if not r.next_run:
            continue
        if today < r.next_run < end:
            val = Decimal(r.valor or 0)
            if (r.tipo or "").upper() == "RECEITA":
                future_receitas_rec += val
            else:
                future_gastos_rec += val

    days_elapsed = max(1, today.day)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_left = max(0, days_in_month - today.day)

    gasto_medio_diario = (gastos_variaveis / Decimal(days_elapsed)) if gastos_variaveis > 0 else Decimal("0")
    estimativa_gastos_restantes = gasto_medio_diario * Decimal(days_left)

    saldo_previsto = saldo_atual + future_receitas_rec - future_gastos_rec - estimativa_gastos_restantes

    return {
        "saldo_atual": saldo_atual,
        "receitas_recorrentes_futuras": future_receitas_rec,
        "gastos_recorrentes_futuros": future_gastos_rec,
        "gasto_medio_diario": gasto_medio_diario,
        "estimativa_gastos_restantes": estimativa_gastos_restantes,
        "saldo_previsto": saldo_previsto,
        "dias_restantes": days_left,
        "alerta_negativo": saldo_previsto < 0,
    }


def calc_alerts(user_id: int, ref_date: date | None = None):
    Transaction, _, _, _ = _models()
    today = ref_date or datetime.utcnow().date()
    start, end = month_bounds(today.year, today.month)

    current_rows = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
        .filter(Transaction.data >= start)
        .filter(Transaction.data < end)
        .all()
    )

    cat_current = {}
    total_gastos = Decimal("0")

    for t in current_rows:
        if (t.tipo or "").upper() != "GASTO":
            continue
        v = Decimal(t.valor or 0)
        total_gastos += v
        cat_current[t.categoria] = cat_current.get(t.categoria, Decimal("0")) + v

    alerts = []
    projection = calc_projection(user_id, today)

    if projection["alerta_negativo"]:
        alerts.append({
            "nivel": "alto",
            "titulo": "Saldo previsto negativo",
            "mensagem": f"Seu saldo projetado para o fim do mês é R$ {fmt_brl(projection['saldo_previsto'])}.",
        })

    if total_gastos > 0:
        cat_top = max(cat_current.items(), key=lambda kv: kv[1])
        if (cat_top[1] / total_gastos) >= Decimal("0.45"):
            alerts.append({
                "nivel": "medio",
                "titulo": f"{cat_top[0]} está pesado no mês",
                "mensagem": f"{cat_top[0]} representa {(cat_top[1] / total_gastos * 100):.0f}% dos seus gastos.",
            })

    for cat, current_value in sorted(cat_current.items(), key=lambda kv: kv[1], reverse=True)[:5]:
        hist_values = []
        for i in range(1, 4):
            base_month = today.month - i
            base_year = today.year
            while base_month <= 0:
                base_month += 12
                base_year -= 1

            h_start, h_end = month_bounds(base_year, base_month)
            rows = (
                Transaction.query
                .filter(Transaction.user_id == user_id)
                .filter(Transaction.tipo == "GASTO")
                .filter(Transaction.data >= h_start)
                .filter(Transaction.data < h_end)
                .filter(Transaction.categoria == cat)
                .all()
            )
            hist_values.append(sum(Decimal(r.valor or 0) for r in rows))

        if hist_values:
            media_hist = sum(hist_values) / Decimal(len(hist_values))
            if media_hist > 0 and current_value >= media_hist * Decimal("1.40"):
                alerts.append({
                    "nivel": "medio",
                    "titulo": f"{cat} acima da média",
                    "mensagem": f"Você gastou R$ {fmt_brl(current_value)} em {cat}; média recente R$ {fmt_brl(media_hist)}.",
                })

    return alerts[:5]


def calc_patrimonio_series(user_id: int, months: int = 6):
    Transaction, Investment, _, _ = _models()
    today = datetime.utcnow().date()
    labels = []
    values = []

    first_month = today.month - (months - 1)
    first_year = today.year
    while first_month <= 0:
        first_month += 12
        first_year -= 1

    running = Decimal("0")

    for offset in range(months):
        month = first_month + offset
        year = first_year
        while month > 12:
            month -= 12
            year += 1

        start, end = month_bounds(year, month)

        txs = (
            Transaction.query
            .filter(Transaction.user_id == user_id)
            .filter(Transaction.data >= start)
            .filter(Transaction.data < end)
            .all()
        )
        invs = (
            Investment.query
            .filter(Investment.user_id == user_id)
            .filter(Investment.data >= start)
            .filter(Investment.data < end)
            .all()
        )

        receitas = sum(Decimal(t.valor or 0) for t in txs if (t.tipo or "").upper() == "RECEITA")
        gastos = sum(Decimal(t.valor or 0) for t in txs if (t.tipo or "").upper() == "GASTO")
        aportes = sum(Decimal(i.valor or 0) for i in invs if (i.tipo or "").upper() == "APORTE")
        resgates = sum(Decimal(i.valor or 0) for i in invs if (i.tipo or "").upper() == "RESGATE")

        running += (receitas - gastos) + (aportes - resgates)

        labels.append(f"{month:02d}/{str(year)[2:]}")
        values.append(float(running))

    return labels, values


def sum_investments_position(user_id: int):
    _, Investment, _, _ = _models()
    invs = Investment.query.filter_by(user_id=user_id).all()
    aportes = Decimal("0")
    resgates = Decimal("0")
    for it in invs:
        v = Decimal(it.valor or 0)
        if (it.tipo or "").upper() == "APORTE":
            aportes += v
        else:
            resgates += v
    patrimonio_investido = aportes - resgates
    return aportes, resgates, patrimonio_investido, invs


def _top_categories_month(user_id: int):
    Transaction, _, _, _ = _models()
    today = datetime.utcnow().date()
    start, end = month_bounds(today.year, today.month)

    rows = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
        .filter(Transaction.tipo == "GASTO")
        .filter(Transaction.data >= start)
        .filter(Transaction.data < end)
        .all()
    )

    cat_map = {}
    total = Decimal("0")
    for t in rows:
        v = Decimal(t.valor or 0)
        total += v
        cat_map[t.categoria] = cat_map.get(t.categoria, Decimal("0")) + v

    ordered = sorted(cat_map.items(), key=lambda kv: kv[1], reverse=True)
    return ordered, total


def build_ai_finance_context(user_id: int) -> str:
    Transaction, Investment, _, _ = _models()
    today = datetime.utcnow().date()
    month_start, month_end = month_bounds(today.year, today.month)
    receitas_mes, gastos_mes, saldo_mes, rows_mes = sum_period(user_id, month_start, month_end)
    proj = calc_projection(user_id, today)
    alerts = calc_alerts(user_id, today)
    aportes, resgates, patrimonio_investido, invs = sum_investments_position(user_id)

    top_cats = {}
    for t in rows_mes:
        if (t.tipo or "").upper() != "GASTO":
            continue
        v = Decimal(t.valor or 0)
        top_cats[t.categoria] = top_cats.get(t.categoria, Decimal("0")) + v

    top_lines = []
    for cat, val in sorted(top_cats.items(), key=lambda kv: kv[1], reverse=True)[:5]:
        top_lines.append(f"- {cat}: R$ {fmt_brl(val)}")

    last_txs = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
        .order_by(Transaction.data.desc(), Transaction.id.desc())
        .limit(8)
        .all()
    )
    tx_lines = []
    for t in last_txs:
        tx_lines.append(f"- {t.data.isoformat()} | {t.tipo} | {t.categoria} | R$ {fmt_brl(t.valor)} | {t.descricao or '-'}")

    last_invs = (
        Investment.query
        .filter(Investment.user_id == user_id)
        .order_by(Investment.data.desc(), Investment.id.desc())
        .limit(5)
        .all()
    )
    inv_lines = []
    for it in last_invs:
        inv_lines.append(f"- {it.data.isoformat()} | {it.tipo} | {it.ativo} | R$ {fmt_brl(it.valor)} | {it.descricao or '-'}")

    alert_lines = [f"- {a['titulo']}: {a['mensagem']}" for a in alerts[:5]]

    return (
        f"Data de referência: {today.isoformat()}\n"
        f"Resumo do mês atual ({today.month:02d}/{today.year}):\n"
        f"- Receitas: R$ {fmt_brl(receitas_mes)}\n"
        f"- Gastos: R$ {fmt_brl(gastos_mes)}\n"
        f"- Saldo: R$ {fmt_brl(saldo_mes)}\n"
        f"- Gasto médio por dia: R$ {fmt_brl(proj['gasto_medio_diario'])}\n"
        f"- Estimativa restante do mês: R$ {fmt_brl(proj['estimativa_gastos_restantes'])}\n"
        f"- Saldo previsto do mês: R$ {fmt_brl(proj['saldo_previsto'])}\n"
        f"- Receitas recorrentes futuras: R$ {fmt_brl(proj['receitas_recorrentes_futuras'])}\n"
        f"- Gastos recorrentes futuros: R$ {fmt_brl(proj['gastos_recorrentes_futuros'])}\n"
        f"- Dias restantes no mês: {proj['dias_restantes']}\n\n"
        f"Investimentos:\n"
        f"- Total aportado: R$ {fmt_brl(aportes)}\n"
        f"- Total resgatado: R$ {fmt_brl(resgates)}\n"
        f"- Patrimônio investido líquido: R$ {fmt_brl(patrimonio_investido)}\n"
        f"- Quantidade de lançamentos de investimento: {len(invs)}\n\n"
        f"Top categorias de gasto no mês:\n" + ("\n".join(top_lines) if top_lines else "- sem dados") + "\n\n"
        f"Alertas atuais:\n" + ("\n".join(alert_lines) if alert_lines else "- nenhum alerta importante") + "\n\n"
        f"Últimos lançamentos financeiros:\n" + ("\n".join(tx_lines) if tx_lines else "- sem lançamentos") + "\n\n"
        f"Últimos investimentos:\n" + ("\n".join(inv_lines) if inv_lines else "- sem investimentos")
    )


def looks_like_finance_question(text_msg: str) -> bool:
    txt = norm_word(text_msg)
    if not txt:
        return False

    keywords = {
        "gastei", "gasto", "gastos", "receita", "receitas", "saldo", "sobrou", "faltando",
        "projecao", "projeção", "alerta", "alertas", "investi", "investido",
        "investimentos", "patrimonio", "patrimônio", "aporte", "resgate", "mercado",
        "categoria", "categorias", "dinheiro", "financeiro", "financas", "finanças",
        "mes", "mês", "semana", "hoje", "quanto", "posso", "tenho", "score", "melhorar"
    }
    return any(k in txt for k in keywords)


def _local_finance_answer(user_id: int, question: str) -> str | None:
    q = norm_word(question)
    today = datetime.utcnow().date()

    receitas_mes, gastos_mes, saldo_mes, _ = sum_period(
        user_id,
        *month_bounds(today.year, today.month)
    )
    proj = calc_projection(user_id, today)
    top_cats, total_gastos = _top_categories_month(user_id)

    if "quanto posso gastar" in q or "posso gastar" in q:
        dias = max(1, proj["dias_restantes"])
        saldo_prev = Decimal(proj["saldo_previsto"] or 0)
        valor_dia = saldo_prev / Decimal(dias) if dias > 0 else Decimal("0")
        return (
            f"💡 Você pode gastar cerca de R$ {fmt_brl(valor_dia)} por dia até o fim do mês, "
            f"considerando o saldo previsto atual de R$ {fmt_brl(saldo_prev)}."
        )

    if "qual categoria" in q and ("mais" in q or "maior" in q):
        if not top_cats:
            return "Ainda não encontrei gastos suficientes no mês para identificar a categoria principal."
        cat, val = top_cats[0]
        pct = (val / total_gastos * 100) if total_gastos > 0 else Decimal("0")
        return f"📊 Sua maior categoria no mês é {cat}, com R$ {fmt_brl(val)} ({pct:.0f}% dos gastos)."

    if "saldo previsto" in q or "saldo final" in q or "como fecho o mês" in q:
        return (
            f"📈 Seu saldo previsto para o fim do mês é R$ {fmt_brl(proj['saldo_previsto'])}. "
            f"Hoje seu saldo do mês está em R$ {fmt_brl(saldo_mes)}."
        )

    if "score" in q and ("melhorar" in q or "como melhorar" in q):
        if not top_cats:
            return "Para melhorar seu score, tente reduzir gastos variáveis e manter o mês com saldo positivo."
        cat, val = top_cats[0]
        sugestao = val * Decimal("0.10")
        return (
            f"🧠 Para melhorar seu score, sua melhor oportunidade agora é reduzir a categoria {cat}. "
            f"Se cortar cerca de R$ {fmt_brl(sugestao)}, seu resultado mensal já melhora."
        )

    if "quanto gastei" in q and ("mes" in q or "mês" in q):
        return f"💸 Neste mês você gastou R$ {fmt_brl(gastos_mes)}."

    if "quanto recebi" in q or ("receitas" in q and ("mes" in q or "mês" in q)):
        return f"💰 Neste mês você recebeu R$ {fmt_brl(receitas_mes)}."

    if "investi" in q or "investimentos" in q or "patrimonio" in q or "patrimônio" in q:
        aportes, resgates, patrimonio_investido, _ = sum_investments_position(user_id)
        return (
            f"💎 Você tem R$ {fmt_brl(patrimonio_investido)} investidos líquidos. "
            f"Aportes: R$ {fmt_brl(aportes)} | Resgates: R$ {fmt_brl(resgates)}."
        )

    if "alerta" in q or "alertas" in q:
        alerts = calc_alerts(user_id, today)
        if not alerts:
            return "✅ Você não tem alertas financeiros importantes no momento."
        a = alerts[0]
        return f"🚨 {a['titulo']}: {a['mensagem']}"

    return None


def ask_openai_finance_assistant(user_id: int, question: str) -> str:
    openai_available = _CFG["openai_available_func"]
    openai_headers = _CFG["openai_headers_func"]

    local_answer = _local_finance_answer(user_id, question)
    if local_answer:
        return local_answer

    if not openai_available or not openai_available():
        return "⚠️ A IA ainda não está ativa no servidor. Configure OPENAI_API_KEY no Railway."

    context = build_ai_finance_context(user_id)
    system = (
        "Você é o Finance AI, um assistente financeiro pessoal dentro de um app. "
        "Responda sempre em português do Brasil, com linguagem clara, objetiva e amigável. "
        "Use SOMENTE o contexto financeiro fornecido. "
        "Se a pergunta fugir de finanças pessoais do usuário, diga que você ajuda apenas com dados financeiros do app. "
        "Não invente valores. Se algo não estiver no contexto, diga explicitamente que não encontrou dados suficientes. "
        "Quando fizer sentido, cite números exatos do contexto e dê uma dica prática curta no final. "
        "Mantenha a resposta curta, no máximo 10 linhas."
    )
    user_prompt = (
        f"Contexto financeiro do usuário:\n{context}\n\n"
        f"Pergunta do usuário:\n{question.strip()}"
    )

    payload = {
        "model": _CFG["openai_chat_model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 350,
    }

    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers=openai_headers(),
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    content = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    content = str(content).strip()
    return content or "Não consegui montar uma resposta agora. Tente novamente em instantes."


def reply_finance_question(user_id: int, text_msg: str) -> str:
    try:
        return ask_openai_finance_assistant(user_id, text_msg)
    except Exception as e:
        print("finance assistant error:", repr(e))
        return "Não consegui responder com a IA agora. Tente novamente em alguns instantes."


def make_resumo_text(user_id: int, kind: str):
    start, end, label = period_range(kind)
    receitas, gastos, saldo, _ = sum_period(user_id, start, end)
    return (
        f"📊 Resumo ({label}):\n"
        f"Receitas: R$ {fmt_brl(receitas)}\n"
        f"Gastos: R$ {fmt_brl(gastos)}\n"
        f"Saldo: R$ {fmt_brl(saldo)}"
    )


def make_analise_text(user_id: int, kind: str | None):
    start, end, label = period_range(kind or "mes")
    receitas, gastos, saldo, rows = sum_period(user_id, start, end)

    cat_map = {}
    biggest = None
    for t in rows:
        v = Decimal(t.valor or 0)
        if (t.tipo or "").upper() != "GASTO":
            continue
        cat_map[t.categoria] = cat_map.get(t.categoria, Decimal("0")) + v
        if biggest is None or v > Decimal(biggest.valor or 0):
            biggest = t

    top = sorted(cat_map.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_lines = []
    total_gastos = Decimal(gastos or 0)
    for cat, val in top:
        pct = (val / total_gastos * 100) if total_gastos > 0 else Decimal("0")
        top_lines.append(f"• {cat}: R$ {fmt_brl(val)} ({pct:.0f}%)")

    today = datetime.utcnow().date()
    proj_line = None
    if label == "este mês":
        days_elapsed = max(1, today.day)
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        daily_avg = (total_gastos / Decimal(days_elapsed)) if total_gastos > 0 else Decimal("0")
        forecast = daily_avg * Decimal(days_in_month)
        proj_line = f"Média/dia: R$ {fmt_brl(daily_avg)} | Projeção mês: R$ {fmt_brl(forecast)}"

    alerts = []
    if total_gastos > 0:
        for cat, val in top[:1]:
            if (val / total_gastos) >= Decimal("0.45"):
                alerts.append(f"⚠️ {cat} está alto ({(val / total_gastos * 100):.0f}% dos gastos).")

    msg = [
        f"🧠 Análise ({label}):",
        f"Receitas: R$ {fmt_brl(receitas)}",
        f"Gastos: R$ {fmt_brl(gastos)}",
        f"Saldo: R$ {fmt_brl(saldo)}",
    ]
    if proj_line:
        msg.append(proj_line)

    if top_lines:
        msg.append("\nTop gastos por categoria:")
        msg.extend(top_lines)

    if biggest and Decimal(biggest.valor or 0) > 0:
        msg.append(f"\nMaior gasto: R$ {fmt_brl(biggest.valor)} em {biggest.categoria} ({biggest.data.isoformat()})")

    if alerts:
        msg.append("\n" + "\n".join(alerts))

    msg.append("\nDica: use 'resumo semana' e 'resumo mês' também.")
    return "\n".join(msg)


def make_projection_text(user_id: int):
    p = calc_projection(user_id)
    lines = [
        "📈 Projeção Finance AI",
        "",
        f"Saldo atual: R$ {fmt_brl(p['saldo_atual'])}",
        f"Receitas recorrentes futuras: R$ {fmt_brl(p['receitas_recorrentes_futuras'])}",
        f"Gastos recorrentes futuros: R$ {fmt_brl(p['gastos_recorrentes_futuros'])}",
        f"Gasto médio/dia: R$ {fmt_brl(p['gasto_medio_diario'])}",
        f"Estimativa do restante do mês: R$ {fmt_brl(p['estimativa_gastos_restantes'])}",
        f"Saldo previsto: R$ {fmt_brl(p['saldo_previsto'])}",
    ]
    if p["alerta_negativo"]:
        lines.append("")
        lines.append("⚠️ Atenção: a projeção indica saldo negativo até o fim do mês.")
    return "\n".join(lines)


def make_alerts_text(user_id: int):
    alerts = calc_alerts(user_id)
    if not alerts:
        return "✅ Nenhum alerta importante no momento."

    lines = ["🚨 Alertas Finance AI", ""]
    for a in alerts:
        lines.append(f"• {a['titulo']}")
        lines.append(f"  {a['mensagem']}")
    return "\n".join(lines)
