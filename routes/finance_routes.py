from flask import request, jsonify


def register_finance_routes(app, db, Transaction, require_login, parse_date_any, parse_brl_value, guess_category_from_text):
    @app.get("/api/lancamentos")
    def api_list_lancamentos():
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        limit = int(request.args.get("limit", 30))
        limit = max(1, min(limit, 200))

        rows = (
            Transaction.query
            .filter(Transaction.user_id == uid)
            .order_by(Transaction.data.desc(), Transaction.id.desc())
            .limit(limit)
            .all()
        )

        items = []
        for t in rows:
            items.append({
                "row": t.id,
                "id": t.id,
                "data": t.data.isoformat() if t.data else None,
                "tipo": t.tipo,
                "categoria": t.categoria,
                "descricao": t.descricao or "",
                "valor": float(t.valor) if t.valor is not None else 0.0,
                "origem": t.origem,
                "criado_em": t.created_at.isoformat() if t.created_at else "",
            })

        return jsonify(items=items)

    @app.post("/api/lancamentos")
    def api_create_lancamento():
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        dataj = request.get_json(silent=True) or {}

        tipo = str(dataj.get("tipo") or "").strip().upper()
        if tipo not in ("RECEITA", "GASTO"):
            return jsonify(error="Tipo inválido"), 400

        descricao = str(dataj.get("descricao") or "").strip() or None
        raw_categoria = str(dataj.get("categoria") or "").strip()
        categoria = raw_categoria.title() if raw_categoria else None
        if not categoria:
            categoria = guess_category_from_text(uid, f"{raw_categoria} {descricao or ''}") or "Outros"

        d = parse_date_any(dataj.get("data"))

        try:
            valor = parse_brl_value(dataj.get("valor"))
        except ValueError as e:
            return jsonify(error=str(e)), 400

        t = Transaction(
            user_id=uid,
            tipo=tipo,
            data=d,
            categoria=categoria,
            descricao=descricao,
            valor=valor,
            origem="APP",
        )
        db.session.add(t)
        db.session.commit()
        return jsonify(ok=True, id=t.id, row=t.id)

    @app.put("/api/lancamentos/<int:row>")
    def api_edit_lancamento(row: int):
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        payload = request.get_json(silent=True) or {}

        t = Transaction.query.filter_by(id=row, user_id=uid).first()
        if not t:
            return jsonify(error="Sem permissão ou inexistente"), 403

        tipo = str(payload.get("tipo") or t.tipo).strip().upper()
        if tipo not in ("RECEITA", "GASTO"):
            return jsonify(error="Tipo inválido"), 400

        t.tipo = tipo
        t.data = parse_date_any(payload.get("data") or t.data.isoformat())
        t.categoria = (str(payload.get("categoria") or t.categoria).strip() or "Outros").title()
        t.descricao = str(payload.get("descricao") or "").strip() or None

        try:
            t.valor = parse_brl_value(payload.get("valor"))
        except ValueError as e:
            return jsonify(error=str(e)), 400

        db.session.commit()
        return jsonify(ok=True)

    @app.delete("/api/lancamentos/<int:row>")
    def api_delete_lancamento(row: int):
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        t = Transaction.query.filter_by(id=row, user_id=uid).first()
        if not t:
            return jsonify(error="Sem permissão ou inexistente"), 403

        db.session.delete(t)
        db.session.commit()
        return jsonify(ok=True)
