from datetime import datetime
from . import db


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    email = db.Column(db.String(120), unique=True, nullable=False)

    password_hash = db.Column(db.String(255), nullable=False)

    name = db.Column(db.String(120))

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )
