from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

db = SQLAlchemy()


def create_app():
    app = Flask(__name__)

    app.config.from_object("config")

    db.init_app(app)
    CORS(app)

    from .routes.auth import auth_bp
    from .routes.account import account_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(account_bp)

    return app
