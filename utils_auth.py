# -*- coding: utf-8 -*-
from flask import session

from utils_core import normalize_email, hash_password


def get_logged_user_id():
    return session.get("user_id")


def get_logged_email():
    return session.get("user_email")


def require_login():
    return get_logged_user_id()


def get_or_create_user_by_email(User, db, email: str, password: str | None = None):
    email = normalize_email(email)
    u = User.query.filter_by(email=email).first()
    if u:
        return u

    if password is None:
        import os
        pw_hash = hash_password(os.urandom(16).hex())
        u = User(email=email, password_hash=pw_hash, password_set=False)
    else:
        u = User(email=email, password_hash=hash_password(password), password_set=True)

    db.session.add(u)
    db.session.commit()
    return u


def login_user(u):
    session["user_id"] = u.id
    session["user_email"] = u.email


def status_payload(
    *,
    db_enabled: bool,
    raw_db_url: str,
    graph_version: str,
    wa_access_token: str,
    wa_phone_number_id: str,
    wa_verify_token: str,
    min_password_len: int,
    openai_api_key: str,
):
    return {
        "ok": True,
        "db_enabled": db_enabled,
        "db_uri_set": bool(raw_db_url),
        "graph_version": graph_version,
        "wa_ready": bool(wa_access_token and wa_phone_number_id and wa_verify_token),
        "min_password_len": min_password_len,
        "openai_ready": bool(openai_api_key),
    }
