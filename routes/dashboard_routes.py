from datetime import datetime, date
from decimal import Decimal

from flask import request, jsonify


def register_dashboard_routes(
    app,
    Transaction,
    require_login,
    calc_projection,
    calc_alerts,
    calc_patrimonio_series,
    looks_like_finance_question=None,
    reply_finance_question=None,
):
    @app.get("/api/dashboard")
    def api_dashboard():
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        try:
            mes = int(request.args.get("mes"))
            ano = int(request.args.get("ano"))
        except Exception:
            return jsonify(error="Parâmetros mes/ano inválidos"), 400

        start = date(ano, mes, 1)
        end = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)

        q = (
            Transaction.query
            .filter(Transaction.user_id == uid)
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

        saldo = receitas - gastos
        return jsonify(
            receitas=float(receitas),
            gastos=float(gastos),
            saldo=float(saldo),
        )

    @app.get("/api/insights_dashboard")
    def api_insights_dashboard():
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        try:
            mes = int(request.args.get("mes", "0"))
            ano = int(request.args.get("ano", "0"))
        except Exception:
            mes = 0
            ano = 0

        today = datetime.utcnow().date()
        if not (1 <= mes <= 12):
            mes = today.month
        if ano < 2000 or ano > 3000:
            ano = today.year

        start = date(ano, mes, 1)
        end = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)

        rows = (
            Transaction.query
            .filter(Transaction.user_id == uid)
            .filter(Transaction.data >= start)
            .filter(Transaction.data < end)
            .all()
        )

        receitas = Decimal("0")
        gastos = Decimal("0")
        categorias = {}

        for t in rows:
            v = Decimal(t.valor or 0)
            if (t.tipo or "").upper() == "RECEITA":
                receitas += v
            else:
                gastos += v
                categorias[t.categoria] = categorias.get(t.categoria, Decimal("0")) + v

        score = 50
        status = "atencao"

        if receitas > 0:
            ratio = gastos / receitas
            if ratio < Decimal("0.50"):
                score = 90
                status = "saudavel"
            elif ratio < Decimal("0.70"):
                score = 80
                status = "saudavel"
            elif ratio < Decimal("0.90"):
                score = 65
                status = "atencao"
            else:
                score = 40
                status = "critico"
        elif gastos > 0:
            score = 25
            status = "critico"

        if not rows:
            insight = "Sem lançamentos no mês selecionado ainda."
            status = "atencao"
        elif gastos > receitas:
            insight = "⚠️ Seus gastos estão maiores que suas receitas neste mês."
            status = "critico"
        elif categorias:
            top = max(categorias.items(), key=lambda x: x[1])
            insight = f"Você gastou mais em {top[0]} neste mês."
        else:
            insight = "Seu controle financeiro está equilibrado."

        top_categorias = sorted(categorias.items(), key=lambda x: x[1], reverse=True)

        return jsonify(
            score=score,
            status=status,
            insight=insight,
            categorias=[c[0] for c in top_categorias],
            valores=[float(c[1]) for c in top_categorias],
            receitas=float(receitas),
            gastos=float(gastos),
        )

    @app.get("/api/projecao")
    def api_projecao():
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        p = calc_projection(uid)
        return jsonify({
            "saldo_atual": float(p["saldo_atual"]),
            "receitas_recorrentes_futuras": float(p["receitas_recorrentes_futuras"]),
            "gastos_recorrentes_futuros": float(p["gastos_recorrentes_futuros"]),
            "gasto_medio_diario": float(p["gasto_medio_diario"]),
            "estimativa_gastos_restantes": float(p["estimativa_gastos_restantes"]),
            "saldo_previsto": float(p["saldo_previsto"]),
            "dias_restantes": p["dias_restantes"],
            "alerta_negativo": p["alerta_negativo"],
        })

    @app.get("/api/alertas")
    def api_alertas():
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401
        return jsonify(items=calc_alerts(uid))

    @app.get("/api/patrimonio")
    def api_patrimonio():
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        months = int(request.args.get("months", "6"))
        months = max(3, min(12, months))

        labels, values = calc_patrimonio_series(uid, months)
        return jsonify(labels=labels, values=values)

    @app.route("/api/score_financeiro")
    def api_score_financeiro():
        uid = require_login()
        if not uid:
            return jsonify({"error": "Não logado"}), 401

        q = Transaction.query.filter(Transaction.user_id == uid).all()

        receitas = sum(Decimal(t.valor or 0) for t in q if (t.tipo or "").upper() == "RECEITA")
        gastos = sum(Decimal(t.valor or 0) for t in q if (t.tipo or "").upper() == "GASTO")
        saldo = receitas - gastos

        score = 50
        status = "atencao"

        if receitas > 0:
            ratio = gastos / receitas
            if ratio < Decimal("0.50"):
                score = 90
                status = "saudavel"
            elif ratio < Decimal("0.70"):
                score = 80
                status = "saudavel"
            elif ratio < Decimal("0.90"):
                score = 65
                status = "atencao"
            else:
                score = 40
                status = "critico"
        elif gastos > 0:
            score = 25
            status = "critico"

        if saldo > 0 and score < 100:
            score = min(100, score + 5)

        return jsonify({
            "score": int(score),
            "status": status,
            "receitas": float(receitas),
            "gastos": float(gastos),
            "saldo": float(saldo),
        })

    @app.post("/api/assistant_finance")
    def api_assistant_finance():
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        if reply_finance_question is None:
            return jsonify(error="Assistente não configurado"), 500

        data = request.get_json(silent=True) or {}
        pergunta = str(data.get("pergunta") or data.get("question") or "").strip()

        if not pergunta:
            return jsonify(error="Pergunta obrigatória"), 400

        if looks_like_finance_question is not None and not looks_like_finance_question(pergunta):
            return jsonify(
                ok=True,
                resposta="Eu posso ajudar com perguntas financeiras do app, como saldo previsto, gastos, categorias, score, investimentos e orçamento."
            )

        try:
            resposta = reply_finance_question(uid, pergunta)
            return jsonify(ok=True, resposta=resposta)
        except Exception as e:
            print("assistant_finance error:", repr(e))
            return jsonify(error="Falha ao processar pergunta"), 500
