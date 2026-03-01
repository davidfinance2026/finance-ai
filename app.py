import os
from datetime import datetime
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
CORS(app)

# =========================
# CONFIGURAÇÃO DATABASE
# =========================

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não definida")

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# =========================
# MODELS
# =========================

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Transaction(db.Model):
    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False)

    date = db.Column(db.Date, nullable=False)
    type = db.Column(db.String(20), nullable=False)
    category = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255))
    value = db.Column(db.Float, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# =========================
# CRIAR TABELAS
# =========================

with app.app_context():
    db.create_all()

# =========================
# ROTAS
# =========================

@app.route("/")
def home():
    return "Finance AI rodando 🚀"


# =========================
# REGISTER
# =========================

@app.route("/api/register", methods=["POST"])
def register():
    data = request.json

    email = data.get("email")
    password = data.get("password")

    if not email or not password:
        return jsonify({"error": "Dados inválidos"}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "Usuário já existe"}), 400

    hashed = generate_password_hash(password)

    user = User(email=email, password=hashed)
    db.session.add(user)
    db.session.commit()

    return jsonify({"message": "Usuário criado"})


# =========================
# LOGIN
# =========================

@app.route("/api/login", methods=["POST"])
def login():
    data = request.json

    email = data.get("email")
    password = data.get("password")

    user = User.query.filter_by(email=email).first()

    if not user or not check_password_hash(user.password, password):
        return jsonify({"error": "Credenciais inválidas"}), 401

    return jsonify({"user_id": user.id, "email": user.email})


# =========================
# NOVO LANÇAMENTO
# =========================

@app.route("/api/lancamentos", methods=["POST"])
def criar_lancamento():
    data = request.json

    user_id = data.get("user_id")
    date_str = data.get("date")
    type_ = data.get("type")
    category = data.get("category")
    description = data.get("description")
    value = data.get("value")

    if not user_id:
        return jsonify({"error": "Usuário não autenticado"}), 401

    try:
        date_obj = datetime.strptime(date_str, "%d/%m/%Y").date()
    except:
        return jsonify({"error": "Data inválida"}), 400

    transaction = Transaction(
        user_id=user_id,
        date=date_obj,
        type=type_,
        category=category,
        description=description,
        value=float(value)
    )

    db.session.add(transaction)
    db.session.commit()

    return jsonify({"message": "Lançamento criado"})


# =========================
# LISTAR LANÇAMENTOS
# =========================

@app.route("/api/lancamentos", methods=["GET"])
def listar_lancamentos():
    user_id = request.args.get("user_id")

    if not user_id:
        return jsonify({"error": "Usuário não autenticado"}), 401

    transactions = (
        Transaction.query
        .filter_by(user_id=user_id)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .all()
    )

    result = []

    for t in transactions:
        result.append({
            "id": t.id,
            "date": t.date.strftime("%d/%m/%Y"),
            "type": t.type,
            "category": t.category,
            "description": t.description,
            "value": t.value
        })

    return jsonify(result)


# =========================
# DASHBOARD
# =========================

@app.route("/api/dashboard", methods=["GET"])
def dashboard():
    user_id = request.args.get("user_id")

    if not user_id:
        return jsonify({"error": "Usuário não autenticado"}), 401

    transactions = Transaction.query.filter_by(user_id=user_id).all()

    receitas = sum(t.value for t in transactions if t.type == "RECEITA")
    gastos = sum(t.value for t in transactions if t.type == "DESPESA")

    return jsonify({
        "receitas": receitas,
        "gastos": gastos,
        "saldo": receitas - gastos
    })


# =========================

if __name__ == "__main__":
    app.run(debug=True)
