from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
def home():
    return "Finance AI online!"

@app.route("/api/dados")
def dados():
    return jsonify({
        "receita": 5000,
        "gasto": 3200,
        "saldo": 1800,
        "categorias": {
            "Alimentação": 800,
            "Transporte": 400,
            "Lazer": 300,
            "Moradia": 1700
        }
    })

app.run(host="0.0.0.0", port=5000)
