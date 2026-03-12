from flask import Blueprint, request, jsonify, session
from ..models import User
from .. import db

auth_bp = Blueprint("auth", __name__)


@auth_bp.post("/api/login")
def login():

    data = request.json

    email = data.get("email")
    password = data.get("senha")

    user = User.query.filter_by(email=email).first()

    if not user:
        return jsonify({"error": "Usuário não encontrado"}), 404

    session["user_id"] = user.id

    return jsonify({
        "email": user.email,
        "name": user.name
    })
