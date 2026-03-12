from flask import request, jsonify
from budget_services import get_budget_summary


def register_budget_routes(app, db, BudgetGoal, require_login, parse_money_br_to_decimal):

    # -------------------------
    # LISTAR ORÇAMENTOS
    # -------------------------
    @app.get("/api/orcamentos")
    def list_orcamentos():
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        try:
            mes = int(request.args.get("mes"))
            ano = int(request.args.get("ano"))
        except Exception:
            return jsonify(error="Parâmetros mes/ano inválidos"), 400

        items = get_budget_summary(uid, ano, mes)

        return jsonify(
            items=items,
            total=len(items)
        )

    # -------------------------
    # ALERTAS DE ORÇAMENTO
    # -------------------------
    @app.get("/api/orcamentos_alertas")
    def orcamentos_alertas():
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        try:
            mes = int(request.args.get("mes"))
            ano = int(request.args.get("ano"))
        except Exception:
            return jsonify(error="Parâmetros mes/ano inválidos"), 400

        items = get_budget_summary(uid, ano, mes)

        alertas = []

        for i in items:
            if i.get("status") in ("atencao", "excedido") or i.get("estoura_meta"):
                alertas.append({
                    "categoria": i.get("categoria"),
                    "mensagem": i.get("mensagem"),
                    "percentual": i.get("percentual"),
                    "projecao_final": i.get("projecao_final")
                })

        return jsonify(
            items=alertas,
            total=len(alertas)
        )

    # -------------------------
    # CRIAR ORÇAMENTO
    # -------------------------
    @app.post("/api/orcamentos")
    def create_orcamento():
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        data = request.get_json(silent=True) or {}

        try:
            categoria = (data.get("categoria") or "TOTAL").title()
            meta = parse_money_br_to_decimal(data.get("valor_meta"))

            ano = int(data.get("ano"))
            mes = int(data.get("mes"))

        except Exception:
            return jsonify(error="Dados inválidos"), 400

        item = BudgetGoal(
            user_id=uid,
            ano=ano,
            mes=mes,
            categoria=categoria,
            valor_meta=meta
        )

        db.session.add(item)
        db.session.commit()

        return jsonify(
            ok=True,
            id=item.id
        )

    # -------------------------
    # APAGAR ORÇAMENTO
    # -------------------------
    @app.delete("/api/orcamentos/<int:id>")
    def delete_orcamento(id):
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        item = BudgetGoal.query.filter_by(
            id=id,
            user_id=uid
        ).first()

        if not item:
            return jsonify(error="Não encontrado"), 404

        db.session.delete(item)
        db.session.commit()

        return jsonify(ok=True)
