from flask import request, jsonify


def register_account_routes(app, db, User, get_logged_user_id, get_logged_email, require_login):
    @app.get("/api/me")
    def api_me():
        uid = get_logged_user_id()
        email = get_logged_email()

        name = None
        if uid:
            u = User.query.filter_by(id=uid).first()
            if u:
                name = u.name

        return jsonify(email=email, user_id=uid, name=name)

    @app.post("/api/account")
    def api_account_update():
        uid = require_login()
        if not uid:
            return jsonify(error="Não logado"), 401

        data = request.get_json(silent=True) or {}
        name = str(data.get("name") or data.get("nome") or "").strip()

        if len(name) > 120:
            return jsonify(error="Nome muito longo"), 400

        u = User.query.filter_by(id=uid).first()
        if not u:
            return jsonify(error="Usuário não encontrado"), 404

        u.name = name or None
        db.session.commit()

        return jsonify(
            ok=True,
            email=u.email,
            name=u.name,
            user_id=u.id
        )
