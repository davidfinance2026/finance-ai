# -*- coding: utf-8 -*-
from datetime import date
from decimal import Decimal

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


def get_budget_summary(user_id, ano, mes):
    start, end = month_bounds(ano, mes)

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
        v = Decimal(t.valor or 0)
        total += v
        cat = (t.categoria or "Outros").title()
        gastos[cat] = gastos.get(cat, Decimal("0")) + v

    metas = (
        _BudgetGoal.query
        .filter_by(user_id=user_id, ano=ano, mes=mes)
        .all()
    )

    items = []

    for m in metas:
        meta = Decimal(m.valor_meta or 0)
        gasto = total if m.categoria == "TOTAL" else gastos.get(m.categoria, Decimal("0"))
        restante = meta - gasto
        percentual = float((gasto / meta) * 100) if meta > 0 else 0

        status = "ok"
        if gasto > meta:
            status = "excedido"
        elif percentual >= 80:
            status = "atencao"

        items.append({
            "id": m.id,
            "categoria": m.categoria,
            "meta": float(meta),
            "gasto": float(gasto),
            "restante": float(restante),
            "percentual": percentual,
            "status": status
        })

    return items
