from flask import request, jsonify


def register_investment_routes(app, db, Investment, require_login, parse_money_br_to_decimal, iso_date):
    @app.get("/api/investimentos")
    def api_investimentos_list():
        user_id = require_login()
        if not user_id:
            return jsonify({"error": "Não logado"}), 401

        limit = int(request.args.get("limit", "50"))
        q = Investment.query.filter_by(user_id=user_id).order_by(Investment.data.desc(), Investment.id.desc())
        items = q.limit(min(limit, 200)).all()

        out = []
        for it in items:
            out.append({
                "id": it.id,
                "data": it.data.isoformat(),
                "ativo": it.ativo,
                "tipo": it.tipo,
                "valor": str(it.valor),
                "descricao": it.descricao or "",
            })
        return jsonify({"items": out})

    @app.post("/api/investimentos")
    def api_investimentos_create():
        user_id = require_login()
        if not user_id:
            return jsonify({"error": "Não logado"}), 401

        data = request.get_json(silent=True) or {}
        ativo = str(data.get("ativo") or "").strip()
        if not ativo:
            return jsonify({"error": "Informe o ativo (ex: Tesouro Selic, PETR4, BTC)."}), 400

        tipo = str(data.get("tipo") or "APORTE").strip().upper()
        if tipo not in ("APORTE", "RESGATE"):
            return jsonify({"error": "Tipo inválido. Use APORTE ou RESGATE."}), 400

        valor = parse_money_br_to_decimal(data.get("valor"))
        if valor <= 0:
            return jsonify({"error": "Informe um valor válido (> 0)."}), 400

        it = Investment(
            user_id=user_id,
            data=iso_date(data.get("data")),
            ativo=ativo,
            tipo=tipo,
            valor=valor,
            descricao=str(data.get("descricao") or "").strip() or None,
        )
        db.session.add(it)
        db.session.commit()
        return jsonify({"ok": True, "id": it.id})

    @app.delete("/api/investimentos/<int:item_id>")
    def api_investimentos_delete(item_id: int):
        user_id = require_login()
        if not user_id:
            return jsonify({"error": "Não logado"}), 401

        it = Investment.query.filter_by(user_id=user_id, id=item_id).first()
        if not it:
            return jsonify({"error": "Investimento não encontrado."}), 404

        db.session.delete(it)
        db.session.commit()
        return jsonify({"ok": True})
