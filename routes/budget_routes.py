from flask import request, jsonify
from budget_services import get_budget_summary


def register_budget_routes(app, db, BudgetGoal, require_login, parse_money_br_to_decimal):

    @app.get("/api/orcamentos")
    def list_orcamentos():
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        mes = int(request.args.get("mes"))
        ano = int(request.args.get("ano"))

        items = get_budget_summary(uid, ano, mes)
        return jsonify(items=items)

    @app.post("/api/orcamentos")
    def create_orcamento():
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        data = request.get_json()

        categoria = data.get("categoria") or "TOTAL"
        meta = parse_money_br_to_decimal(data.get("valor_meta"))

        item = BudgetGoal(
            user_id=uid,
            ano=int(data.get("ano")),
            mes=int(data.get("mes")),
            categoria=categoria.title(),
            valor_meta=meta
        )

        db.session.add(item)
        db.session.commit()

        return jsonify(ok=True, id=item.id)

    @app.delete("/api/orcamentos/<int:id>")
    def delete_orcamento(id):
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        item = BudgetGoal.query.filter_by(id=id, user_id=uid).first()

        if not item:
            return jsonify(error="Não encontrado"), 404

        db.session.delete(item)
        db.session.commit()

        return jsonify(ok=True)
