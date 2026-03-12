# -*- coding: utf-8 -*-
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

_BudgetGoal = None
_Transaction = None


def init_budget_services(BudgetGoal, Transaction):
    global _BudgetGoal, _Transaction
    _BudgetGoal = BudgetGoal
    _Transaction = Transaction


def month_bounds(year, month):
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _to_decimal(v):
    return Decimal(v or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _days_in_month(year, month):
    start, end = month_bounds(year, month)
    return (end - start).days


def _safe_float(v):
    return float(_to_decimal(v))


def _build_projection(meta, gasto, gasto_medio_diario, dias_restantes):
    projeção_final = gasto + (gasto_medio_diario * Decimal(dias_restantes))
    restante_proj = meta - projeção_final

    estoura = projeção_final > meta if meta > 0 else False
    dias_para_estourar = None

    if meta > 0 and gasto_medio_diario > 0 and gasto < meta:
        faltante = meta - gasto
        dias_est = faltante / gasto_medio_diario
        dias_para_estourar = int(dias_est.to_integral_value(rounding=ROUND_HALF_UP))
        if dias_para_estourar <= 0:
            dias_para_estourar = 1

    return {
        "projecao_final": _to_decimal(projeção_final),
        "restante_projetado": _to_decimal(restante_proj),
        "estoura_meta": estoura,
        "dias_para_estourar": dias_para_estourar,
    }


def _build_budget_message(categoria, status, percentual, estoura_meta, dias_para_estourar, restante, projeção_final, meta):
    categoria_txt = categoria or "Categoria"

    if meta <= 0:
        return f"{categoria_txt}: meta inválida ou não definida."

    if status == "excedido":
        return f"🚨 {categoria_txt}: meta já ultrapassada."

    if estoura_meta and dias_para_estourar:
        return f"⚠️ {categoria_txt}: neste ritmo, a meta deve estourar em cerca de {dias_para_estourar} dia(s)."

    if estoura_meta:
        return f"⚠️ {categoria_txt}: a projeção do mês ultrapassa a meta."

    if percentual >= 80:
        return f"🟡 {categoria_txt}: você já usou {int(percentual)}% da meta."

    if restante >= 0:
        return f"✅ {categoria_txt}: meta sob controle. Projeção do mês: R$ {float(projeção_final):.2f}"

    return f"{categoria_txt}: acompanhe os gastos desta categoria."


def get_budget_summary(user_id, ano, mes):
    start, end = month_bounds(ano, mes)
    hoje = date.today()

    txs = (
        _Transaction.query
        .filter(_Transaction.user_id == user_id)
        .filter(_Transaction.tipo == "GASTO")
        .filter(_Transaction.data >= start)
        .filter(_Transaction.data < end)
        .all()
    )

    gastos = {}
    total = Decimal("0")

    for t in txs:
        v = _to_decimal(t.valor)
        total += v
        cat = (t.categoria or "Outros").title()
        gastos[cat] = gastos.get(cat, Decimal("0")) + v

    metas = (
        _BudgetGoal.query
        .filter_by(user_id=user_id, ano=ano, mes=mes)
        .all()
    )

    days_month = _days_in_month(ano, mes)

    if hoje.year == ano and hoje.month == mes:
        dias_passados = max(1, hoje.day)
        dias_restantes = max(0, days_month - hoje.day)
    else:
        dias_passados = days_month
        dias_restantes = 0

    items = []

    for m in metas:
        meta = _to_decimal(m.valor_meta)
        gasto = total if m.categoria == "TOTAL" else gastos.get(m.categoria, Decimal("0"))
        gasto = _to_decimal(gasto)

        restante = _to_decimal(meta - gasto)
        percentual = float((gasto / meta) * 100) if meta > 0 else 0.0

        status = "ok"
        if gasto > meta:
            status = "excedido"
        elif percentual >= 80:
            status = "atencao"

        gasto_medio_diario = _to_decimal(gasto / Decimal(dias_passados)) if dias_passados > 0 else Decimal("0.00")

        proj = _build_projection(
            meta=meta,
            gasto=gasto,
            gasto_medio_diario=gasto_medio_diario,
            dias_restantes=dias_restantes,
        )

        mensagem = _build_budget_message(
            categoria=m.categoria,
            status=status,
            percentual=percentual,
            estoura_meta=proj["estoura_meta"],
            dias_para_estourar=proj["dias_para_estourar"],
            restante=restante,
            projeção_final=proj["projecao_final"],
            meta=meta,
        )

        items.append({
            "id": m.id,
            "categoria": m.categoria,
            "meta": _safe_float(meta),
            "gasto": _safe_float(gasto),
            "restante": _safe_float(restante),
            "percentual": percentual,
            "status": status,
            "gasto_medio_diario": _safe_float(gasto_medio_diario),
            "dias_restantes": dias_restantes,
            "projecao_final": _safe_float(proj["projecao_final"]),
            "restante_projetado": _safe_float(proj["restante_projetado"]),
            "estoura_meta": proj["estoura_meta"],
            "dias_para_estourar": proj["dias_para_estourar"],
            "mensagem": mensagem,
        })

    return items
