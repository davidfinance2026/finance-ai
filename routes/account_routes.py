from flask import request, jsonify, session


def register_auth_routes(app, db, User, MIN_PASSWORD_LEN, normalize_email, hash_password, login_user):
    @app.post("/api/register")
    def api_register():
        data = request.get_json(silent=True) or {}

        nome = str(
            data.get("nome_completo")
            or data.get("nome")
            or data.get("name")
            or data.get("nome_apelido")
            or ""
        ).strip()

        email = normalize_email(data.get("email"))
        senha = str(data.get("senha") or data.get("password") or "")
        confirmar = str(data.get("confirmar_senha") or data.get("confirmar") or data.get("confirm") or "")

        if not email or "@" not in email:
            return jsonify(error="Email inválido"), 400
        if len(senha) < MIN_PASSWORD_LEN:
            return jsonify(error=f"Senha deve ter pelo menos {MIN_PASSWORD_LEN} caracteres"), 400
        if senha != confirmar:
            return jsonify(error="Senhas não conferem"), 400

        existing = User.query.filter_by(email=email).first()
        if existing:
            if getattr(existing, "password_set", False) is False:
                existing.password_hash = hash_password(senha)
                existing.password_set = True
                if nome and not existing.name:
                    existing.name = nome
                db.session.commit()
                login_user(existing)
                return jsonify(email=existing.email, name=existing.name, claimed=True)
            return jsonify(error="Email já cadastrado"), 400

        u = User(
            email=email,
            name=nome or None,
            password_hash=hash_password(senha),
            password_set=True,
        )
        db.session.add(u)
        db.session.commit()

        login_user(u)
        return jsonify(email=u.email, name=u.name)

    @app.post("/api/login")
    def api_login():
        data = request.get_json(silent=True) or {}
        email = normalize_email(data.get("email"))
        senha = str(data.get("senha") or data.get("password") or "")

        u = User.query.filter_by(email=email).first()
        if not u or u.password_hash != hash_password(senha):
            return jsonify(error="Email ou senha inválidos"), 401

        login_user(u)
        return jsonify(email=u.email, name=u.name)

    @app.post("/api/logout")
    def api_logout():
        session.clear()
        return jsonify(ok=True)

    @app.post("/api/reset_password")
    def api_reset_password():
        data = request.get_json(silent=True) or {}
        email = normalize_email(data.get("email"))
        nova = str(data.get("nova_senha") or data.get("newPassword") or data.get("password") or "")
        confirmar = str(data.get("confirmar") or data.get("confirm") or "")

        if not email or "@" not in email:
            return jsonify(error="Email inválido"), 400
        if len(nova) < MIN_PASSWORD_LEN:
            return jsonify(error=f"Senha deve ter pelo menos {MIN_PASSWORD_LEN} caracteres"), 400
        if nova != confirmar:
            return jsonify(error="Senhas não conferem"), 400

        u = User.query.filter_by(email=email).first()
        if not u:
            return jsonify(error="Email não encontrado"), 404

        u.password_hash = hash_password(nova)
        u.password_set = True
        db.session.commit()
        return jsonify(ok=True)
