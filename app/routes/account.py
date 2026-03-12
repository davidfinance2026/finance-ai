from flask import Blueprint, request, jsonify, session
from ..models import User
from .. import db

account_bp = Blueprint("account", __name__)


@account_bp.get("/api/me")
def me():

    user_id = session.get("user_id")

    if not user_id:
        return jsonify({"email": None})

    user = User.query.get(user_id)

    return jsonify({
        "email": user.email,
        "name": user.name
    })


@account_bp.post("/api/account")
def update_account():

    user_id = session.get("user_id")

    if not user_id:
        return jsonify({"error": "Não autenticado"}), 401

    data = request.json

    user = User.query.get(user_id)

    user.name = data.get("name")

    db.session.commit()

    return jsonify({"name": user.name})
