# -*- coding: utf-8 -*-
import os
import re
import json
import hashlib
import calendar
import base64
import tempfile
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask, request, jsonify, send_from_directory, session, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, inspect
from sqlalchemy.exc import SQLAlchemyError
from urllib.parse import quote
from PyPDF2 import PdfReader

# -------------------------
# App / Config
# -------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
app.config["JSON_AS_ASCII"] = False

# Cookies de sessão (Railway/HTTPS)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = os.getenv("SESSION_SAMESITE", "Lax")
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_SECURE", "1") == "1"

# DB
_raw_db_url = os.getenv("DATABASE_URL", "").strip()
if _raw_db_url.startswith("postgres://"):
    _raw_db_url = _raw_db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = _raw_db_url or ("sqlite:///" + os.path.join(BASE_DIR, "local.db"))
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
    "pool_size": int(os.getenv("DB_POOL_SIZE", "3")),
    "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "2")),
}
DB_ENABLED = bool(_raw_db_url)

# Senha mínima
MIN_PASSWORD_LEN = int(os.getenv("MIN_PASSWORD_LEN", "6"))

# WhatsApp Cloud API (Meta)
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "").strip()
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "").strip()
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "").strip()
GRAPH_VERSION = os.getenv("GRAPH_VERSION", "v20.0").strip()

# Botão de pânico (token opcional)
PANIC_TOKEN = os.getenv("PANIC_TOKEN", "").strip()

# WhatsApp público
WA_PUBLIC_NUMBER = os.getenv("WA_PUBLIC_NUMBER", "5537998675231").strip()

# OpenAI (áudio / imagem / PDF)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1-mini").strip()
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", OPENAI_CHAT_MODEL).strip()
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe").strip()

# -------------------------
# DB
# -------------------------
db = SQLAlchemy(app)


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(64), nullable=False)
    password_set = db.Column(db.Boolean, nullable=False, server_default=text("false"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Transaction(db.Model):
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    tipo = db.Column(db.String(16), nullable=False)  # RECEITA/GASTO
    data = db.Column(db.Date, nullable=False, index=True)
    categoria = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    valor = db.Column(db.Numeric(12, 2), nullable=False)
    origem = db.Column(db.String(16), nullable=False, default="APP")  # APP/WA/REC
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Investment(db.Model):
    __tablename__ = "investments"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    data = db.Column(db.Date, nullable=False)
    ativo = db.Column(db.String(120), nullable=False)
    tipo = db.Column(db.String(20), nullable=False, default="APORTE")  # APORTE | RESGATE
    valor = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    descricao = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("investments", lazy=True))


class WaLink(db.Model):
    __tablename__ = "wa_links"
    id = db.Column(db.Integer, primary_key=True)
    wa_from = db.Column(db.String(40), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ProcessedMessage(db.Model):
    __tablename__ = "processed_messages"
    id = db.Column(db.Integer, primary_key=True)
    msg_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    wa_from = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class CategoryRule(db.Model):
    __tablename__ = "category_rules"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    pattern = db.Column(db.String(80), nullable=False)
    categoria = db.Column(db.String(80), nullable=False)
    priority = db.Column(db.Integer, nullable=False, server_default=text("10"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class WaPending(db.Model):
    __tablename__ = "wa_pending"
    id = db.Column(db.Integer, primary_key=True)
    wa_from = db.Column(db.String(40), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    kind = db.Column(db.String(40), nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class RecurringRule(db.Model):
    __tablename__ = "recurring_rules"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    freq = db.Column(db.String(16), nullable=False)  # DAILY/WEEKLY/MONTHLY
    day_of_month = db.Column(db.Integer, nullable=True)
    weekday = db.Column(db.Integer, nullable=True)  # 0=seg ... 6=dom

    tipo = db.Column(db.String(16), nullable=False)  # RECEITA/GASTO
    valor = db.Column(db.Numeric(12, 2), nullable=False)
    categoria = db.Column(db.String(80), nullable=False)
    descricao = db.Column(db.Text, nullable=True)

    start_date = db.Column(db.Date, nullable=False, default=lambda: datetime.utcnow().date())
    next_run = db.Column(db.Date, nullable=False, index=True)
    is_active = db.Column(db.Boolean, nullable=False, server_default=text("true"))

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


def _create_tables_if_needed():
    try:
        db.create_all()
    except Exception as e:
        print("DB create_all failed:", repr(e))


def _bootstrap_schema():
    """Migração leve/idempotente (sem Alembic)."""
    try:
        insp = inspect(db.engine)
        dialect = db.engine.dialect.name

        def has_table(t: str) -> bool:
            try:
                return insp.has_table(t)
            except Exception:
                return t in insp.get_table_names()

        def has_col(t: str, c: str) -> bool:
            if not has_table(t):
                return False
            cols = {col.get("name") for col in insp.get_columns(t)}
            return c in cols

        def add_col(t: str, col_name: str, col_ddl: str):
            if dialect == "postgresql":
                db.session.execute(text(
                    f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {col_name} {col_ddl}"
                ))
                db.session.commit()
                return

            if has_col(t, col_name):
                return

            try:
                db.session.execute(text(f"ALTER TABLE {t} ADD COLUMN {col_name} {col_ddl}"))
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()

        if has_table("recurring_rules"):
            add_col("recurring_rules", "start_date", "DATE")
            add_col("recurring_rules", "weekday", "INTEGER")
            add_col("recurring_rules", "day_of_month", "INTEGER")
            add_col("recurring_rules", "next_run", "DATE")
            add_col("recurring_rules", "is_active", "BOOLEAN DEFAULT TRUE")
            add_col("recurring_rules", "tipo", "VARCHAR(16)")
            add_col("recurring_rules", "valor", "NUMERIC(12,2)")
            add_col("recurring_rules", "categoria", "VARCHAR(80)")
            add_col("recurring_rules", "descricao", "TEXT")

            if dialect == "postgresql":
                if has_col("recurring_rules", "next_date"):
                    try:
                        db.session.execute(text("""
                            ALTER TABLE recurring_rules
                            ALTER COLUMN next_date DROP NOT NULL
                        """))
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

                    try:
                        db.session.execute(text("""
                            UPDATE recurring_rules
                            SET next_run = COALESCE(next_run, next_date)
                            WHERE next_run IS NULL AND next_date IS NOT NULL
                        """))
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

                try:
                    db.session.execute(text("""
                        UPDATE recurring_rules
                        SET start_date = COALESCE(start_date, CURRENT_DATE)
                        WHERE start_date IS NULL
                    """))
                    db.session.commit()
                except Exception:
                    db.session.rollback()

                try:
                    db.session.execute(text("""
                        UPDATE recurring_rules
                        SET next_run = COALESCE(next_run, CURRENT_DATE)
                        WHERE next_run IS NULL
                    """))
                    db.session.commit()
                except Exception:
                    db.session.rollback()

    except Exception as e:
        print("DB bootstrap_schema failed:", repr(e))
        try:
            db.session.rollback()
        except Exception:
            pass


with app.app_context():
    _create_tables_if_needed()
    _bootstrap_schema()

# -------------------------
# Helpers
# -------------------------
def _hash_password(pw: str) -> str:
    return hashlib.sha256((pw or "").encode("utf-8")).hexdigest()


def _normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def _get_logged_user_id():
    return session.get("user_id")


def _get_logged_email():
    return session.get("user_email")


def _require_login():
    return _get_logged_user_id()


def _parse_brl_value(v) -> Decimal:
    if v is None:
        raise ValueError("valor vazio")
    s = str(v).strip()
    if not s:
        raise ValueError("valor vazio")

    s = re.sub(r"[^0-9,\.-]", "", s)

    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        if "," in s and "." not in s:
            s = s.replace(",", ".")

    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError("valor inválido")


def _parse_date_any(v) -> date:
    if not v:
        return datetime.utcnow().date()
    s = str(v).strip()
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return datetime.strptime(s, "%Y-%m-%d").date()
        if re.match(r"^\d{2}/\d{2}/\d{4}$", s):
            return datetime.strptime(s, "%d/%m/%Y").date()
        if re.match(r"^\d{2}-\d{2}-\d{4}$", s):
            return datetime.strptime(s, "%d-%m-%Y").date()
    except Exception:
        pass
    return datetime.utcnow().date()


def _parse_money_br_to_decimal(value):
    s = str(value or "").strip()
    if not s:
        return Decimal("0")
    s = s.replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return Decimal(s)
    except Exception:
        return Decimal("0")


def _iso_date(value):
    s = str(value or "").strip()
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return datetime.utcnow().date()


def _get_or_create_user_by_email(email: str, password: str | None = None) -> User:
    email = _normalize_email(email)
    u = User.query.filter_by(email=email).first()
    if u:
        return u

    if password is None:
        pw_hash = _hash_password(os.urandom(16).hex())
        u = User(email=email, password_hash=pw_hash, password_set=False)
    else:
        u = User(email=email, password_hash=_hash_password(password), password_set=True)

    db.session.add(u)
    db.session.commit()
    return u


def _login_user(u: User):
    session["user_id"] = u.id
    session["user_email"] = u.email


def _status_payload():
    return {
        "ok": True,
        "db_enabled": DB_ENABLED,
        "db_uri_set": bool(_raw_db_url),
        "graph_version": GRAPH_VERSION,
        "wa_ready": bool(WA_ACCESS_TOKEN and WA_PHONE_NUMBER_ID and WA_VERIFY_TOKEN),
        "min_password_len": MIN_PASSWORD_LEN,
        "openai_ready": bool(OPENAI_API_KEY),
    }


def _normalize_wa_number(raw: str) -> str:
    s = (raw or "").strip().replace("+", "")
    s = re.sub(r"[^0-9]", "", s)
    return s


def wa_send_text(to_number: str, text_msg: str):
    to_number = _normalize_wa_number(to_number)
    if not (WA_PHONE_NUMBER_ID and WA_ACCESS_TOKEN and to_number):
        print("WA send skipped (missing creds or number). msg:", text_msg)
        return

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": str(text_msg or "")[:3900]},
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        if r.status_code >= 400:
            print("WA send error:", r.status_code, r.text)
    except Exception as e:
        print("WA send exception:", repr(e))


def _openai_headers() -> dict:
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }


def _openai_available() -> bool:
    return bool(OPENAI_API_KEY)


def _download_whatsapp_media(media_id: str, fallback_name: str = "arquivo"):
    if not media_id or not WA_ACCESS_TOKEN:
        raise ValueError("mídia indisponível")

    meta_url = f"https://graph.facebook.com/{GRAPH_VERSION}/{media_id}"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}"}
    r = requests.get(meta_url, headers=headers, timeout=20)
    r.raise_for_status()
    meta = r.json()

    dl_url = meta.get("url")
    mime_type = meta.get("mime_type") or "application/octet-stream"
    ext_map = {
        "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
        "image/jpeg": ".jpg", "image/png": ".png",
        "application/pdf": ".pdf",
    }
    ext = ext_map.get(mime_type, "")

    r2 = requests.get(dl_url, headers=headers, timeout=60)
    r2.raise_for_status()

    fd, tmp_path = tempfile.mkstemp(prefix="wa_media_", suffix=ext)
    with os.fdopen(fd, "wb") as f:
        f.write(r2.content)

    return tmp_path, mime_type, os.path.basename(tmp_path) or fallback_name


def _transcribe_audio_file(file_path: str) -> str:
    if not _openai_available():
        raise RuntimeError("OPENAI_API_KEY não configurada")

    with open(file_path, "rb") as f:
        files = {"file": (os.path.basename(file_path), f, "application/octet-stream")}
        data = {"model": OPENAI_TRANSCRIBE_MODEL}
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files=files,
            data=data,
            timeout=120,
        )
    r.raise_for_status()
    return (r.json().get("text") or "").strip()


def _extract_pdf_text(file_path: str) -> str:
    try:
        reader = PdfReader(file_path)
        chunks = []
        for page in reader.pages[:10]:
            chunks.append(page.extract_text() or "")
        return "\n".join(chunks).strip()
    except Exception:
        return ""


def _extract_json_from_text(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _normalize_ai_result(obj: dict) -> dict | None:
    if not obj:
        return None
    try:
        valor = _parse_brl_value(obj.get("valor"))
    except Exception:
        return None

    tipo = str(obj.get("tipo") or "").strip().upper()
    if tipo not in ("RECEITA", "GASTO"):
        return None

    categoria = (str(obj.get("categoria") or "").strip() or "Outros").title()
    descricao = str(obj.get("descricao") or "").strip() or None
    confidence = str(obj.get("confidence") or obj.get("confianca") or "medium").strip().lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"

    return {
        "tipo": tipo,
        "valor": str(valor),
        "categoria": categoria,
        "descricao": descricao,
        "data": _parse_date_any(obj.get("data")).isoformat(),
        "confidence": confidence,
        "justificativa": str(obj.get("justificativa") or "").strip(),
    }


def _call_openai_finance_json(user_prompt: str, image_base64: str | None = None, mime_type: str | None = None) -> dict | None:
    if not _openai_available():
        raise RuntimeError("OPENAI_API_KEY não configurada")

    system = (
        "Você é um extrator financeiro. Analise comprovantes, conversas e descrições em português do Brasil. "
        "Retorne SOMENTE JSON válido com as chaves: tipo, valor, categoria, descricao, data, confidence, justificativa. "
        "tipo deve ser RECEITA ou GASTO. valor numérico em formato brasileiro ou ponto decimal. "
        "confidence deve ser high, medium ou low. "
        "Se houver pix enviado/pagamento/compra, normalmente é GASTO. Se houver pix recebido/recebi/depósito recebido, normalmente é RECEITA."
    )

    content = [{"type": "text", "text": user_prompt}]
    if image_base64 and mime_type:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{image_base64}"}
        })

    payload = {
        "model": OPENAI_VISION_MODEL if image_base64 else OPENAI_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content if image_base64 else user_prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    r = requests.post("https://api.openai.com/v1/chat/completions", headers=_openai_headers(), json=payload, timeout=120)
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"]
    return _normalize_ai_result(_extract_json_from_text(raw))


def _analyze_text_transaction(text_value: str, source_name: str = "texto") -> dict | None:
    txt = (text_value or "").strip()
    if not txt:
        return None
    prompt = (
        f"Analise o conteúdo abaixo vindo de {source_name} e extraia um lançamento financeiro.\n\n"
        f"Conteúdo:\n{txt}"
    )
    return _call_openai_finance_json(prompt)


def _analyze_image_transaction(file_path: str, mime_type: str) -> dict | None:
    with open(file_path, "rb") as f:
        img64 = base64.b64encode(f.read()).decode("utf-8")
    prompt = (
        "Analise esta imagem de comprovante, recibo, nota ou print bancário. "
        "Extraia um único lançamento financeiro mais provável."
    )
    return _call_openai_finance_json(prompt, image_base64=img64, mime_type=mime_type)


def _save_ai_transaction(user_id: int, tx_data: dict, origem: str = "WA") -> Transaction:
    tx = Transaction(
        user_id=user_id,
        tipo=tx_data["tipo"],
        data=_parse_date_any(tx_data.get("data")),
        categoria=(tx_data.get("categoria") or "Outros").title(),
        descricao=tx_data.get("descricao") or None,
        valor=_parse_brl_value(tx_data.get("valor")),
        origem=origem,
    )
    db.session.add(tx)
    db.session.commit()
    return tx


def _pending_confirmation_choice(text_msg: str) -> str | None:
    norm = _norm_word(text_msg)
    if norm in ("1", "sim", "s", "confirmar", "ok"):
        return "confirm"
    if norm in ("2", "nao", "não", "n", "cancelar", "cancela"):
        return "cancel"
    return None


def _handle_pending_ai_confirmation(wa_from: str, user_id: int, text_msg: str) -> bool:
    choice = _pending_confirmation_choice(text_msg)
    if not choice:
        return False
    pending = _pending_get(wa_from)
    if not pending or pending.user_id != user_id or pending.kind != "CONFIRM_AI_TX":
        return False

    if choice == "cancel":
        _pending_clear(wa_from, user_id)
        wa_send_text(wa_from, "❌ Lançamento cancelado. Pode enviar outro comprovante, PDF, foto ou áudio.")
        return True

    payload = json.loads(pending.payload_json or "{}")
    tx_data = payload.get("tx") or {}
    tx = _save_ai_transaction(user_id, tx_data, origem="WA")
    _pending_clear(wa_from, user_id)
    wa_send_text(wa_from,
        "✅ Lançamento salvo!\n"
        f"ID: {tx.id}\n"
        f"Tipo: {tx.tipo}\n"
        f"Valor: R$ {_fmt_brl(tx.valor)}\n"
        f"Categoria: {tx.categoria}\n"
        f"Data: {tx.data.isoformat()}"
    )
    return True


def _send_ai_confirmation_request(wa_from: str, user_id: int, tx_data: dict, source_label: str):
    _pending_set(wa_from, user_id, "CONFIRM_AI_TX", {"tx": tx_data, "source": source_label}, minutes=15)
    wa_send_text(wa_from,
        "🤖 Identifiquei este lançamento, mas quero sua confirmação:\n\n"
        f"Tipo: {tx_data['tipo']}\n"
        f"Valor: R$ {_fmt_brl(tx_data['valor'])}\n"
        f"Categoria: {tx_data['categoria']}\n"
        f"Descrição: {tx_data.get('descricao') or '-'}\n"
        f"Data: {tx_data['data']}\n\n"
        "Responda com:\n"
        "1 = confirmar\n"
        "2 = cancelar"
    )


def _handle_whatsapp_media_message(link: WaLink, wa_from: str, msg: dict) -> bool:
    msg_type = msg.get("type")
    if msg_type not in ("audio", "image", "document"):
        return False

    if not _openai_available():
        wa_send_text(wa_from, "⚠️ O reconhecimento por IA ainda não está ativo no servidor. Configure OPENAI_API_KEY no Railway.")
        return True

    media_obj = (msg.get(msg_type) or {})
    media_id = media_obj.get("id")
    tmp_path = None

    try:
        tmp_path, mime_type, _ = _download_whatsapp_media(media_id, msg_type)

        tx_data = None
        source_label = msg_type

        if msg_type == "audio":
            transcript = _transcribe_audio_file(tmp_path)
            if not transcript:
                wa_send_text(wa_from, "Não consegui transcrever esse áudio. Tente novamente com um áudio mais curto e claro.")
                return True
            tx_data = _analyze_text_transaction(transcript, "áudio transcrito")
            source_label = f"áudio: {transcript}"

        elif msg_type == "image":
            tx_data = _analyze_image_transaction(tmp_path, mime_type)
            source_label = "imagem/comprovante"

        elif msg_type == "document":
            if mime_type == "application/pdf":
                pdf_text = _extract_pdf_text(tmp_path)
                if pdf_text:
                    tx_data = _analyze_text_transaction(pdf_text, "PDF de comprovante")
                else:
                    wa_send_text(wa_from, "Li o PDF, mas não consegui extrair texto suficiente para lançar. Tente enviar print ou foto do comprovante.")
                    return True
                source_label = "PDF"
            else:
                wa_send_text(wa_from, "Por enquanto consigo interpretar PDF, imagem e áudio. Esse documento ainda não é suportado.")
                return True

        if not tx_data:
            wa_send_text(wa_from, "Não consegui identificar um lançamento confiável nesse arquivo. Pode enviar outro comprovante ou escrever em texto.")
            return True

        if tx_data.get("confidence") == "high":
            tx = _save_ai_transaction(link.user_id, tx_data, origem="WA")
            wa_send_text(wa_from,
                "✅ Lançamento salvo pela IA!\n"
                f"ID: {tx.id}\n"
                f"Tipo: {tx.tipo}\n"
                f"Valor: R$ {_fmt_brl(tx.valor)}\n"
                f"Categoria: {tx.categoria}\n"
                f"Data: {tx.data.isoformat()}"
            )
        else:
            _send_ai_confirmation_request(wa_from, link.user_id, tx_data, source_label)

        return True

    except Exception as e:
        print("WA media handle error:", repr(e))
        wa_send_text(wa_from, "Não consegui processar essa mídia agora. Tente novamente em alguns instantes.")
        return True
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _fmt_brl(v: Decimal | float | int | None) -> str:
    try:
        d = Decimal(v or 0)
    except Exception:
        d = Decimal("0")
    s = f"{d:.2f}"
    return s.replace(".", ",")


def _month_bounds(year: int, month: int):
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _period_range(kind: str):
    today = datetime.utcnow().date()
    k = _norm_word(kind)
    if k in ("hoje", "dia"):
        start = today
        end = today + timedelta(days=1)
        label = "hoje"
        return start, end, label
    if k == "semana":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
        label = "esta semana"
        return start, end, label

    start = date(today.year, today.month, 1)
    if today.month == 12:
        end = date(today.year + 1, 1, 1)
    else:
        end = date(today.year, today.month + 1, 1)
    label = "este mês"
    return start, end, label


def _sum_period(user_id: int, start: date, end: date):
    q = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
        .filter(Transaction.data >= start)
        .filter(Transaction.data < end)
        .all()
    )
    receitas = Decimal("0")
    gastos = Decimal("0")
    for t in q:
        v = Decimal(t.valor or 0)
        if (t.tipo or "").upper() == "RECEITA":
            receitas += v
        else:
            gastos += v
    return receitas, gastos, (receitas - gastos), q


def _calc_projection(user_id: int, ref_date: date | None = None):
    today = ref_date or datetime.utcnow().date()
    start, end = _month_bounds(today.year, today.month)

    rows = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
        .filter(Transaction.data >= start)
        .filter(Transaction.data < end)
        .all()
    )

    receitas = Decimal("0")
    gastos = Decimal("0")
    gastos_variaveis = Decimal("0")

    for t in rows:
        v = Decimal(t.valor or 0)
        if (t.tipo or "").upper() == "RECEITA":
            receitas += v
        else:
            gastos += v
            if (t.origem or "").upper() != "REC":
                gastos_variaveis += v

    saldo_atual = receitas - gastos

    future_receitas_rec = Decimal("0")
    future_gastos_rec = Decimal("0")

    recurring_rules = (
        RecurringRule.query
        .filter(RecurringRule.user_id == user_id, RecurringRule.is_active == True)
        .all()
    )

    for r in recurring_rules:
        if not r.next_run:
            continue
        if today < r.next_run < end:
            val = Decimal(r.valor or 0)
            if (r.tipo or "").upper() == "RECEITA":
                future_receitas_rec += val
            else:
                future_gastos_rec += val

    days_elapsed = max(1, today.day)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_left = max(0, days_in_month - today.day)

    gasto_medio_diario = (gastos_variaveis / Decimal(days_elapsed)) if gastos_variaveis > 0 else Decimal("0")
    estimativa_gastos_restantes = gasto_medio_diario * Decimal(days_left)

    saldo_previsto = saldo_atual + future_receitas_rec - future_gastos_rec - estimativa_gastos_restantes

    return {
        "saldo_atual": saldo_atual,
        "receitas_recorrentes_futuras": future_receitas_rec,
        "gastos_recorrentes_futuros": future_gastos_rec,
        "gasto_medio_diario": gasto_medio_diario,
        "estimativa_gastos_restantes": estimativa_gastos_restantes,
        "saldo_previsto": saldo_previsto,
        "dias_restantes": days_left,
        "alerta_negativo": saldo_previsto < 0,
    }


def _calc_alerts(user_id: int, ref_date: date | None = None):
    today = ref_date or datetime.utcnow().date()
    start, end = _month_bounds(today.year, today.month)

    current_rows = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
        .filter(Transaction.data >= start)
        .filter(Transaction.data < end)
        .all()
    )

    cat_current = {}
    total_gastos = Decimal("0")

    for t in current_rows:
        if (t.tipo or "").upper() != "GASTO":
            continue
        v = Decimal(t.valor or 0)
        total_gastos += v
        cat_current[t.categoria] = cat_current.get(t.categoria, Decimal("0")) + v

    alerts = []
    projection = _calc_projection(user_id, today)

    if projection["alerta_negativo"]:
        alerts.append({
            "nivel": "alto",
            "titulo": "Saldo previsto negativo",
            "mensagem": f"Seu saldo projetado para o fim do mês é R$ {_fmt_brl(projection['saldo_previsto'])}.",
        })

    if total_gastos > 0:
        cat_top = max(cat_current.items(), key=lambda kv: kv[1])
        if (cat_top[1] / total_gastos) >= Decimal("0.45"):
            alerts.append({
                "nivel": "medio",
                "titulo": f"{cat_top[0]} está pesado no mês",
                "mensagem": f"{cat_top[0]} representa {(cat_top[1] / total_gastos * 100):.0f}% dos seus gastos.",
            })

    for cat, current_value in sorted(cat_current.items(), key=lambda kv: kv[1], reverse=True)[:5]:
        hist_values = []
        for i in range(1, 4):
            base_month = today.month - i
            base_year = today.year
            while base_month <= 0:
                base_month += 12
                base_year -= 1

            h_start, h_end = _month_bounds(base_year, base_month)
            rows = (
                Transaction.query
                .filter(Transaction.user_id == user_id)
                .filter(Transaction.tipo == "GASTO")
                .filter(Transaction.data >= h_start)
                .filter(Transaction.data < h_end)
                .filter(Transaction.categoria == cat)
                .all()
            )
            hist_values.append(sum(Decimal(r.valor or 0) for r in rows))

        if hist_values:
            media_hist = sum(hist_values) / Decimal(len(hist_values))
            if media_hist > 0 and current_value >= media_hist * Decimal("1.40"):
                alerts.append({
                    "nivel": "medio",
                    "titulo": f"{cat} acima da média",
                    "mensagem": f"Você gastou R$ {_fmt_brl(current_value)} em {cat}; média recente R$ {_fmt_brl(media_hist)}.",
                })

    return alerts[:5]


def _calc_patrimonio_series(user_id: int, months: int = 6):
    today = datetime.utcnow().date()
    labels = []
    values = []

    first_month = today.month - (months - 1)
    first_year = today.year
    while first_month <= 0:
        first_month += 12
        first_year -= 1

    running = Decimal("0")

    for offset in range(months):
        month = first_month + offset
        year = first_year
        while month > 12:
            month -= 12
            year += 1

        start, end = _month_bounds(year, month)

        txs = (
            Transaction.query
            .filter(Transaction.user_id == user_id)
            .filter(Transaction.data >= start)
            .filter(Transaction.data < end)
            .all()
        )
        invs = (
            Investment.query
            .filter(Investment.user_id == user_id)
            .filter(Investment.data >= start)
            .filter(Investment.data < end)
            .all()
        )

        receitas = sum(Decimal(t.valor or 0) for t in txs if (t.tipo or "").upper() == "RECEITA")
        gastos = sum(Decimal(t.valor or 0) for t in txs if (t.tipo or "").upper() == "GASTO")
        aportes = sum(Decimal(i.valor or 0) for i in invs if (i.tipo or "").upper() == "APORTE")
        resgates = sum(Decimal(i.valor or 0) for i in invs if (i.tipo or "").upper() == "RESGATE")

        running += (receitas - gastos) + (aportes - resgates)

        labels.append(f"{month:02d}/{str(year)[2:]}")
        values.append(float(running))

    return labels, values


def _norm_word(w: str) -> str:
    w = (w or "").strip().lower()
    w = (
        w.replace("á", "a").replace("à", "a").replace("â", "a").replace("ã", "a")
         .replace("é", "e").replace("ê", "e")
         .replace("í", "i")
         .replace("ó", "o").replace("ô", "o").replace("õ", "o")
         .replace("ú", "u")
         .replace("ç", "c")
    )
    return w


def _tokenize(textv: str) -> list[str]:
    textv = _norm_word(textv)
    parts = re.split(r"[^a-z0-9]+", textv)
    return [p for p in parts if p]


def _guess_category_from_text(user_id: int, full_text: str) -> str | None:
    tokens = set(_tokenize(full_text))

    try:
        rules = (
            CategoryRule.query
            .filter(CategoryRule.user_id == user_id)
            .order_by(CategoryRule.priority.desc(), CategoryRule.id.desc())
            .all()
        )
        for r in rules:
            key = _norm_word(r.pattern)
            if not key:
                continue
            if key in tokens or any(key in t for t in tokens):
                return (r.categoria or "").strip().title() or None
    except Exception:
        pass

    default_category_keywords = [
        ("Alimentação", {"ifood", "i-food", "restaurante", "lanchonete", "pizza", "burguer", "hamburguer", "lanche", "mercado", "padaria", "cafe", "café"}),
        ("Transporte", {"uber", "99", "taxi", "táxi", "onibus", "ônibus", "metro", "metrô", "gasolina", "etanol", "combustivel", "combustível", "estacionamento"}),
        ("Moradia", {"aluguel", "condominio", "condomínio", "iptu", "prestacao", "prestação", "financiamento", "luz", "energia", "agua", "água", "internet"}),
        ("Saúde", {"farmacia", "farmácia", "remedio", "remédio", "medico", "médico", "consulta", "exame", "dentista"}),
        ("Educação", {"curso", "faculdade", "escola", "mensalidade", "livro"}),
        ("Lazer", {"cinema", "show", "bar", "viagem", "hotel"}),
        ("Impostos", {"imposto", "taxa", "multa"}),
        ("Transferências", {"pix", "ted", "doc", "transferencia", "transferência"}),
    ]

    for cat, keys in default_category_keywords:
        nkeys = {_norm_word(k) for k in keys}
        if tokens & nkeys:
            return cat

    return None

# -------------------------
# Static / Frontend
# -------------------------
@app.get("/")
def home():
    return render_template("index.html")


@app.get("/offline.html")
def offline_page():
    return render_template("offline.html")


@app.get("/manifest.json")
def manifest():
    return send_from_directory(app.static_folder, "manifest.json")


@app.get("/sw.js")
def service_worker():
    resp = send_from_directory(app.static_folder, "sw.js")
    resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    return resp


@app.get("/robots.txt")
def robots():
    return send_from_directory(app.static_folder, "robots.txt")


@app.get("/health")
def health():
    return jsonify(_status_payload())


@app.get("/api/wa_link")
def api_wa_link():
    uid = _get_logged_user_id()
    email = _get_logged_email()

    to = _normalize_wa_number(WA_PUBLIC_NUMBER)
    if not uid or not email:
        return jsonify(url=f"https://wa.me/{to}")

    text_msg = f"conectar {email}"
    url = f"https://wa.me/{to}?text={quote(text_msg)}"
    return jsonify(url=url)


@app.get("/wa")
def wa_shortcut():
    uid = _get_logged_user_id()
    email = _get_logged_email()
    to = _normalize_wa_number(WA_PUBLIC_NUMBER)

    if uid and email:
        text_msg = f"conectar {email}"
        url = f"https://wa.me/{to}?text={quote(text_msg)}"
    else:
        url = f"https://wa.me/{to}"

    return ("", 302, {"Location": url})


# -------------------------
# Panic Reset
# -------------------------
def _panic_allowed() -> bool:
    if not PANIC_TOKEN:
        return True
    token = (
        request.headers.get("X-Panic-Token")
        or request.args.get("token")
        or (request.get_json(silent=True) or {}).get("token")
        or ""
    )
    return str(token) == PANIC_TOKEN


@app.route("/api/panic_reset", methods=["GET", "POST"])
def api_panic_reset():
    if not _panic_allowed():
        return jsonify({"error": "forbidden"}), 403

    try:
        db.session.execute(
            text(
                "TRUNCATE TABLE processed_messages, wa_links, transactions, category_rules, wa_pending, recurring_rules, investments, users "
                "RESTART IDENTITY CASCADE;"
            )
        )
        db.session.commit()
        return jsonify({"ok": True, "message": "Banco limpo."})
    except Exception:
        db.session.rollback()

    try:
        ProcessedMessage.query.delete()
        WaLink.query.delete()
        Transaction.query.delete()
        CategoryRule.query.delete()
        WaPending.query.delete()
        RecurringRule.query.delete()
        Investment.query.delete()
        User.query.delete()
        db.session.commit()
        return jsonify({"ok": True, "message": "Banco limpo (fallback)."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "panic_reset_failed", "detail": str(e)}), 500


# -------------------------
# Auth API
# -------------------------
@app.post("/api/register")
def api_register():
    data = request.get_json(silent=True) or {}

    email = _normalize_email(data.get("email"))
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
            existing.password_hash = _hash_password(senha)
            existing.password_set = True
            db.session.commit()
            _login_user(existing)
            return jsonify(email=existing.email, claimed=True)
        return jsonify(error="Email já cadastrado"), 400

    u = _get_or_create_user_by_email(email, password=senha)
    _login_user(u)
    return jsonify(email=u.email)


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    email = _normalize_email(data.get("email"))
    senha = str(data.get("senha") or data.get("password") or "")

    u = User.query.filter_by(email=email).first()
    if not u or u.password_hash != _hash_password(senha):
        return jsonify(error="Email ou senha inválidos"), 401

    _login_user(u)
    return jsonify(email=u.email)


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/reset_password")
def api_reset_password():
    data = request.get_json(silent=True) or {}
    email = _normalize_email(data.get("email"))
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

    u.password_hash = _hash_password(nova)
    u.password_set = True
    db.session.commit()
    return jsonify(ok=True)


@app.get("/api/me")
def api_me():
    return jsonify(email=_get_logged_email(), user_id=_get_logged_user_id())


# -------------------------
# Transactions API
# -------------------------
@app.get("/api/lancamentos")
def api_list_lancamentos():
    uid = _require_login()
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
        items.append(
            {
                "row": t.id,
                "id": t.id,
                "data": t.data.isoformat() if t.data else None,
                "tipo": t.tipo,
                "categoria": t.categoria,
                "descricao": t.descricao or "",
                "valor": float(t.valor) if t.valor is not None else 0.0,
                "origem": t.origem,
                "criado_em": t.created_at.isoformat() if t.created_at else "",
            }
        )

    return jsonify(items=items)


@app.post("/api/lancamentos")
def api_create_lancamento():
    uid = _require_login()
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
        categoria = _guess_category_from_text(uid, f"{raw_categoria} {descricao or ''}") or "Outros"

    d = _parse_date_any(dataj.get("data"))

    try:
        valor = _parse_brl_value(dataj.get("valor"))
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
    uid = _require_login()
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
    t.data = _parse_date_any(payload.get("data") or t.data.isoformat())
    t.categoria = (str(payload.get("categoria") or t.categoria).strip() or "Outros").title()
    t.descricao = str(payload.get("descricao") or "").strip() or None

    try:
        t.valor = _parse_brl_value(payload.get("valor"))
    except ValueError as e:
        return jsonify(error=str(e)), 400

    db.session.commit()
    return jsonify(ok=True)


@app.delete("/api/lancamentos/<int:row>")
def api_delete_lancamento(row: int):
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    t = Transaction.query.filter_by(id=row, user_id=uid).first()
    if not t:
        return jsonify(error="Sem permissão ou inexistente"), 403

    db.session.delete(t)
    db.session.commit()
    return jsonify(ok=True)


# -----------------------
# Investimentos
# -----------------------
@app.get("/api/investimentos")
def api_investimentos_list():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Não logado"}), 401

    limit = int(request.args.get("limit", "50"))
    q = Investment.query.filter_by(user_id=user_id).order_by(Investment.data.desc(), Investment.id.desc())
    items = q.limit(min(limit, 200)).all()
    out = []
    for it in items:
        out.append({
            "id": it.id,
            "data": it.data.isoformat(),
            "ativo": it.ativo,
            "tipo": it.tipo,
            "valor": str(it.valor),
            "descricao": it.descricao or "",
        })
    return jsonify({"items": out})


@app.post("/api/investimentos")
def api_investimentos_create():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Não logado"}), 401

    data = request.get_json(silent=True) or {}
    ativo = str(data.get("ativo") or "").strip()
    if not ativo:
        return jsonify({"error": "Informe o ativo (ex: Tesouro Selic, PETR4, BTC)."}), 400

    tipo = str(data.get("tipo") or "APORTE").strip().upper()
    if tipo not in ("APORTE", "RESGATE"):
        return jsonify({"error": "Tipo inválido. Use APORTE ou RESGATE."}), 400

    valor = _parse_money_br_to_decimal(data.get("valor"))
    if valor <= 0:
        return jsonify({"error": "Informe um valor válido (> 0)."}), 400

    it = Investment(
        user_id=user_id,
        data=_iso_date(data.get("data")),
        ativo=ativo,
        tipo=tipo,
        valor=valor,
        descricao=str(data.get("descricao") or "").strip() or None,
    )
    db.session.add(it)
    db.session.commit()
    return jsonify({"ok": True, "id": it.id})


@app.delete("/api/investimentos/<int:item_id>")
def api_investimentos_delete(item_id: int):
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Não logado"}), 401

    it = Investment.query.filter_by(user_id=user_id, id=item_id).first()
    if not it:
        return jsonify({"error": "Investimento não encontrado."}), 404

    db.session.delete(it)
    db.session.commit()
    return jsonify({"ok": True})


# -------------------------
# Dashboard + IA v3
# -------------------------
@app.get("/api/dashboard")
def api_dashboard():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    try:
        mes = int(request.args.get("mes"))
        ano = int(request.args.get("ano"))
    except Exception:
        return jsonify(error="Parâmetros mes/ano inválidos"), 400

    start = date(ano, mes, 1)
    end = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)

    q = (
        Transaction.query
        .filter(Transaction.user_id == uid)
        .filter(Transaction.data >= start)
        .filter(Transaction.data < end)
        .all()
    )

    receitas = Decimal("0")
    gastos = Decimal("0")
    for t in q:
        v = Decimal(t.valor or 0)
        if (t.tipo or "").upper() == "RECEITA":
            receitas += v
        else:
            gastos += v

    saldo = receitas - gastos
    return jsonify(receitas=float(receitas), gastos=float(gastos), saldo=float(saldo))
# -----------------------
# Insights Inteligentes
# -----------------------
@app.get("/api/insights_dashboard")
def api_insights_dashboard():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    try:
        mes = int(request.args.get("mes", "0"))
        ano = int(request.args.get("ano", "0"))
    except Exception:
        mes = 0
        ano = 0

    today = datetime.utcnow().date()
    if not (1 <= mes <= 12):
        mes = today.month
    if ano < 2000 or ano > 3000:
        ano = today.year

    start = date(ano, mes, 1)
    end = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)

    rows = (
        Transaction.query
        .filter(Transaction.user_id == uid)
        .filter(Transaction.data >= start)
        .filter(Transaction.data < end)
        .all()
    )

    receitas = Decimal("0")
    gastos = Decimal("0")
    categorias = {}

    for t in rows:
        v = Decimal(t.valor or 0)
        if (t.tipo or "").upper() == "RECEITA":
            receitas += v
        else:
            gastos += v
            categorias[t.categoria] = categorias.get(t.categoria, Decimal("0")) + v

    score = 50
    status = "atencao"

    if receitas > 0:
        ratio = gastos / receitas
        if ratio < Decimal("0.50"):
            score = 90
            status = "saudavel"
        elif ratio < Decimal("0.70"):
            score = 80
            status = "saudavel"
        elif ratio < Decimal("0.90"):
            score = 65
            status = "atencao"
        else:
            score = 40
            status = "critico"
    elif gastos > 0:
        score = 25
        status = "critico"

    if not rows:
        insight = "Sem lançamentos no mês selecionado ainda."
        status = "atencao"
    elif gastos > receitas:
        insight = "⚠️ Seus gastos estão maiores que suas receitas neste mês."
        status = "critico"
    elif categorias:
        top = max(categorias.items(), key=lambda x: x[1])
        insight = f"Você gastou mais em {top[0]} neste mês."
    else:
        insight = "Seu controle financeiro está equilibrado."

    top_categorias = sorted(categorias.items(), key=lambda x: x[1], reverse=True)

    return jsonify(
        score=score,
        status=status,
        insight=insight,
        categorias=[c[0] for c in top_categorias],
        valores=[float(c[1]) for c in top_categorias],
        receitas=float(receitas),
        gastos=float(gastos),
    )

@app.get("/api/projecao")
def api_projecao():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    p = _calc_projection(uid)
    return jsonify({
        "saldo_atual": float(p["saldo_atual"]),
        "receitas_recorrentes_futuras": float(p["receitas_recorrentes_futuras"]),
        "gastos_recorrentes_futuros": float(p["gastos_recorrentes_futuros"]),
        "gasto_medio_diario": float(p["gasto_medio_diario"]),
        "estimativa_gastos_restantes": float(p["estimativa_gastos_restantes"]),
        "saldo_previsto": float(p["saldo_previsto"]),
        "dias_restantes": p["dias_restantes"],
        "alerta_negativo": p["alerta_negativo"],
    })


@app.get("/api/alertas")
def api_alertas():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401
    return jsonify(items=_calc_alerts(uid))


@app.get("/api/patrimonio")
def api_patrimonio():
    uid = _require_login()
    if not uid:
        return jsonify(error="Não logado"), 401

    months = int(request.args.get("months", "6"))
    months = max(3, min(12, months))
    labels, values = _calc_patrimonio_series(uid, months)
    return jsonify(labels=labels, values=values)

# -------------------------
# WhatsApp - inteligência
# -------------------------
CONNECT_ALIASES = ("conectar", "vincular", "linkar", "associar", "registrar", "conexao", "conexão")
NEGATIONS = {"nao", "não", "nunca", "jamais"}

VALUE_RE = re.compile(r"([+\-])?\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:[.,]\d{1,2})?)")

INCOME_HINTS = {
    "recebi", "recebido", "recebida", "entrou", "entrada", "caiu",
    "deposito", "depósito", "salario", "salário", "venda", "vendido",
    "comissao", "comissão", "bonus", "bônus", "reembolso", "ganhei", "renda", "receita",
    "pixrecebido", "pix_recebido",
}
EXPENSE_HINTS = {
    "paguei", "pago", "pagar", "comprei", "compra", "gastei", "gasto", "despesa",
    "saida", "saída", "debito", "débito", "boleto", "conta", "fatura", "cartao", "cartão",
}

DEFAULT_CATEGORY_KEYWORDS = [
    ("Alimentação", {"ifood", "i-food", "restaurante", "lanchonete", "pizza", "burguer", "hamburguer", "lanche", "mercado", "padaria", "cafe", "café"}),
    ("Transporte", {"uber", "99", "taxi", "táxi", "onibus", "ônibus", "metro", "metrô", "gasolina", "etanol", "combustivel", "combustível", "estacionamento"}),
    ("Moradia", {"aluguel", "condominio", "condomínio", "iptu", "prestacao", "prestação", "financiamento", "luz", "energia", "agua", "água", "internet"}),
    ("Saúde", {"farmacia", "farmácia", "remedio", "remédio", "medico", "médico", "consulta", "exame", "dentista"}),
    ("Educação", {"curso", "faculdade", "escola", "mensalidade", "livro"}),
    ("Lazer", {"cinema", "show", "bar", "viagem", "hotel"}),
    ("Impostos", {"imposto", "taxa", "multa"}),
    ("Transferências", {"pix", "ted", "doc", "transferencia", "transferência"}),
]

CMD_HELP_RE = re.compile(r"^\s*(ajuda|\?|help)\s*$", re.IGNORECASE)
CMD_ULTIMOS_RE = re.compile(r"^\s*ultimos\s*$", re.IGNORECASE)
CMD_APAGAR_RE = re.compile(r"^\s*apagar\s+(\d+)\s*$", re.IGNORECASE)
CMD_CORRIGIR_ULTIMA_RE = re.compile(r"^\s*corrigir\s+ultima\s+(.+)$", re.IGNORECASE)
CMD_EDITAR_RE = re.compile(r"^\s*editar\s+(\d+)\s+(.+)$", re.IGNORECASE)
CMD_DESFAZER_RE = re.compile(r"^\s*desfazer\s*$", re.IGNORECASE)

CMD_RESUMO_RE = re.compile(r"^\s*resumo\s+(hoje|dia|semana|mes|m[eê]s)\s*$", re.IGNORECASE)
CMD_SALDO_MES_RE = re.compile(r"^\s*saldo\s+m[eê]s\s*$", re.IGNORECASE)
CMD_ANALISE_RE = re.compile(r"^\s*(analise|an[aá]lise|insights)\s*(hoje|semana|mes|m[eê]s)?\s*$", re.IGNORECASE)
CMD_PROJECAO_RE = re.compile(r"^\s*(projecao|projeção)\s*$", re.IGNORECASE)
CMD_ALERTAS_RE = re.compile(r"^\s*alertas?\s*$", re.IGNORECASE)

CAT_SET_RE = re.compile(r"^\s*categoria\s+(.+?)\s*=\s*(.+?)\s*$", re.IGNORECASE)
CAT_DEL_RE = re.compile(r"^\s*remover\s+categoria\s+(.+?)\s*$", re.IGNORECASE)
CAT_LIST_RE = re.compile(r"^\s*categorias\s*$", re.IGNORECASE)

REC_ADD_RE = re.compile(r"^\s*recorrente\s+(diario|di[aá]rio|semanal|mensal)\s+(.+)$", re.IGNORECASE)
REC_LIST_RE = re.compile(r"^\s*recorrentes\s*$", re.IGNORECASE)
REC_DEL_RE = re.compile(r"^\s*remover\s+recorrente\s+(\d+)\s*$", re.IGNORECASE)
REC_RUN_RE = re.compile(r"^\s*(gerar|rodar)\s+recorrentes\s*$", re.IGNORECASE)

WEEKDAY_MAP = {
    "seg": 0, "segunda": 0,
    "ter": 1, "terça": 1, "terca": 1,
    "qua": 2, "quarta": 2,
    "qui": 3, "quinta": 3,
    "sex": 4, "sexta": 4,
    "sab": 5, "sábado": 5, "sabado": 5,
    "dom": 6, "domingo": 6,
}


def _detect_tipo_with_score(sign: str, before_tokens: list[str], after_tokens: list[str]):
    if sign == "+":
        return "RECEITA", "high"
    if sign == "-":
        return "GASTO", "high"

    bset = set(before_tokens)
    aset = set(after_tokens)

    income_set = {_norm_word(x) for x in INCOME_HINTS}
    expense_set = {_norm_word(x) for x in EXPENSE_HINTS}

    b_income = len(bset & income_set)
    b_exp = len(bset & expense_set)
    a_income = len(aset & income_set)
    a_exp = len(aset & expense_set)

    score_income = (b_income * 3) + a_income
    score_exp = (b_exp * 3) + a_exp

    has_neg = any(t in {_norm_word(n) for n in NEGATIONS} for n in NEGATIONS for t in before_tokens[:2])
    if has_neg and score_income > 0 and score_exp == 0:
        score_income = 0

    if score_income == 0 and score_exp == 0:
        return "GASTO", "low"
    if score_income == score_exp:
        return ("RECEITA" if score_income > 0 else "GASTO"), "low"
    if score_income > score_exp:
        return "RECEITA", ("high" if (score_income - score_exp) >= 2 else "low")
    return "GASTO", ("high" if (score_exp - score_income) >= 2 else "low")


def _wa_help_text():
    return (
        "✅ Comandos disponíveis:\n\n"
        "🔗 Conectar:\n"
        "• conectar seuemail@dominio.com\n\n"
        "🧾 Lançar:\n"
        "• recebi 1200 salario\n"
        "• paguei 32,90 mercado\n"
        "• + 35,90 venda camiseta\n"
        "• - 18,00 uber\n\n"
        "📊 Inteligência Finance AI:\n"
        "• projeção\n"
        "• alertas\n"
        "• analise\n"
        "• analise semana\n"
        "• analise mês\n"
        "• quanto gastei esse mês\n"
        "• quanto tenho investido\n"
        "• qual meu saldo previsto\n\n"
        "🧠 Se houver dúvida, eu pergunto: RECEITA ou GASTO.\n"
        "Responda apenas: receita  (ou)  gasto\n\n"
        "✏️ Corrigir aqui:\n"
        "• ultimos\n"
        "• apagar 123\n"
        "• editar 123 valor=35,90 categoria=Alimentação data=2026-03-01 descricao=\"algo\" tipo=receita\n"
        "• corrigir ultima categoria=Transporte\n\n"
        "↩️ Desfazer (janela de 5 min):\n"
        "• desfazer\n\n"
        "📊 Resumos:\n"
        "• resumo hoje\n"
        "• resumo semana\n"
        "• resumo mês\n"
        "• saldo mês\n\n"
        "🔁 Recorrentes:\n"
        "• recorrente mensal 5 1200 aluguel\n"
        "• recorrente semanal seg 50 academia\n"
        "• recorrente diário 10 cafe\n"
        "• recorrentes\n"
        "• remover recorrente 7\n"
        "• rodar recorrentes\n\n"
        "🏷️ Ensinar categorias:\n"
        "• categorias\n"
        "• categoria ifood = Alimentação\n"
        "• remover categoria ifood\n"
    )


def _make_resumo_text(user_id: int, kind: str):
    start, end, label = _period_range(kind)
    receitas, gastos, saldo, _ = _sum_period(user_id, start, end)
    return (
        f"📊 Resumo ({label}):\n"
        f"Receitas: R$ {_fmt_brl(receitas)}\n"
        f"Gastos: R$ {_fmt_brl(gastos)}\n"
        f"Saldo: R$ {_fmt_brl(saldo)}"
    )


def _make_analise_text(user_id: int, kind: str | None):
    start, end, label = _period_range(kind or "mes")
    receitas, gastos, saldo, rows = _sum_period(user_id, start, end)

    cat_map = {}
    biggest = None
    for t in rows:
        v = Decimal(t.valor or 0)
        if (t.tipo or "").upper() != "GASTO":
            continue
        cat_map[t.categoria] = cat_map.get(t.categoria, Decimal("0")) + v
        if biggest is None or v > Decimal(biggest.valor or 0):
            biggest = t

    top = sorted(cat_map.items(), key=lambda kv: kv[1], reverse=True)[:5]
    top_lines = []
    total_gastos = Decimal(gastos or 0)
    for cat, val in top:
        pct = (val / total_gastos * 100) if total_gastos > 0 else Decimal("0")
        top_lines.append(f"• {cat}: R$ {_fmt_brl(val)} ({pct:.0f}%)")

    today = datetime.utcnow().date()
    if label == "este mês":
        days_elapsed = max(1, today.day)
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        daily_avg = (total_gastos / Decimal(days_elapsed)) if total_gastos > 0 else Decimal("0")
        forecast = daily_avg * Decimal(days_in_month)
        proj_line = f"Média/dia: R$ {_fmt_brl(daily_avg)} | Projeção mês: R$ {_fmt_brl(forecast)}"
    else:
        proj_line = None

    alerts = []
    if total_gastos > 0:
        for cat, val in top[:1]:
            if (val / total_gastos) >= Decimal("0.45"):
                alerts.append(f"⚠️ {cat} está alto ({(val/total_gastos*100):.0f}% dos gastos).")

    msg = [
        f"🧠 Análise ({label}):",
        f"Receitas: R$ {_fmt_brl(receitas)}",
        f"Gastos: R$ {_fmt_brl(gastos)}",
        f"Saldo: R$ {_fmt_brl(saldo)}",
    ]
    if proj_line:
        msg.append(proj_line)

    if top_lines:
        msg.append("\nTop gastos por categoria:")
        msg.extend(top_lines)

    if biggest and Decimal(biggest.valor or 0) > 0:
        msg.append(
            f"\nMaior gasto: R$ {_fmt_brl(biggest.valor)} em {biggest.categoria} ({biggest.data.isoformat()})"
        )

    if alerts:
        msg.append("\n" + "\n".join(alerts))

    msg.append("\nDica: use 'resumo semana' e 'resumo mês' também.")
    return "\n".join(msg)


def _make_projection_text(user_id: int):
    p = _calc_projection(user_id)
    lines = [
        "📈 Projeção Finance AI",
        "",
        f"Saldo atual: R$ {_fmt_brl(p['saldo_atual'])}",
        f"Receitas recorrentes futuras: R$ {_fmt_brl(p['receitas_recorrentes_futuras'])}",
        f"Gastos recorrentes futuros: R$ {_fmt_brl(p['gastos_recorrentes_futuros'])}",
        f"Gasto médio/dia: R$ {_fmt_brl(p['gasto_medio_diario'])}",
        f"Estimativa do restante do mês: R$ {_fmt_brl(p['estimativa_gastos_restantes'])}",
        f"Saldo previsto: R$ {_fmt_brl(p['saldo_previsto'])}",
    ]
    if p["alerta_negativo"]:
        lines.append("")
        lines.append("⚠️ Atenção: a projeção indica saldo negativo até o fim do mês.")
    return "\n".join(lines)


def _make_alerts_text(user_id: int):
    alerts = _calc_alerts(user_id)
    if not alerts:
        return "✅ Nenhum alerta importante no momento."

    lines = ["🚨 Alertas Finance AI", ""]
    for a in alerts:
        lines.append(f"• {a['titulo']}")
        lines.append(f"  {a['mensagem']}")
    return "\n".join(lines)


def _sum_investments_position(user_id: int):
    invs = Investment.query.filter_by(user_id=user_id).all()
    aportes = Decimal("0")
    resgates = Decimal("0")
    for it in invs:
        v = Decimal(it.valor or 0)
        if (it.tipo or "").upper() == "APORTE":
            aportes += v
        else:
            resgates += v
    patrimonio_investido = aportes - resgates
    return aportes, resgates, patrimonio_investido, invs


def _build_ai_finance_context(user_id: int) -> str:
    today = datetime.utcnow().date()
    month_start, month_end = _month_bounds(today.year, today.month)
    receitas_mes, gastos_mes, saldo_mes, rows_mes = _sum_period(user_id, month_start, month_end)
    proj = _calc_projection(user_id, today)
    alerts = _calc_alerts(user_id, today)
    aportes, resgates, patrimonio_investido, invs = _sum_investments_position(user_id)

    top_cats = {}
    for t in rows_mes:
        if (t.tipo or "").upper() != "GASTO":
            continue
        v = Decimal(t.valor or 0)
        top_cats[t.categoria] = top_cats.get(t.categoria, Decimal("0")) + v
    top_lines = []
    for cat, val in sorted(top_cats.items(), key=lambda kv: kv[1], reverse=True)[:5]:
        top_lines.append(f"- {cat}: R$ {_fmt_brl(val)}")

    last_txs = (
        Transaction.query
        .filter(Transaction.user_id == user_id)
        .order_by(Transaction.data.desc(), Transaction.id.desc())
        .limit(8)
        .all()
    )
    tx_lines = []
    for t in last_txs:
        tx_lines.append(
            f"- {t.data.isoformat()} | {t.tipo} | {t.categoria} | R$ {_fmt_brl(t.valor)} | {t.descricao or '-'}"
        )

    last_invs = (
        Investment.query
        .filter(Investment.user_id == user_id)
        .order_by(Investment.data.desc(), Investment.id.desc())
        .limit(5)
        .all()
    )
    inv_lines = []
    for it in last_invs:
        inv_lines.append(
            f"- {it.data.isoformat()} | {it.tipo} | {it.ativo} | R$ {_fmt_brl(it.valor)} | {it.descricao or '-'}"
        )

    alert_lines = [f"- {a['titulo']}: {a['mensagem']}" for a in alerts[:5]]

    return (
        f"Data de referência: {today.isoformat()}\n"
        f"Resumo do mês atual ({today.month:02d}/{today.year}):\n"
        f"- Receitas: R$ {_fmt_brl(receitas_mes)}\n"
        f"- Gastos: R$ {_fmt_brl(gastos_mes)}\n"
        f"- Saldo: R$ {_fmt_brl(saldo_mes)}\n"
        f"- Gasto médio por dia: R$ {_fmt_brl(proj['gasto_medio_diario'])}\n"
        f"- Estimativa restante do mês: R$ {_fmt_brl(proj['estimativa_gastos_restantes'])}\n"
        f"- Saldo previsto do mês: R$ {_fmt_brl(proj['saldo_previsto'])}\n"
        f"- Receitas recorrentes futuras: R$ {_fmt_brl(proj['receitas_recorrentes_futuras'])}\n"
        f"- Gastos recorrentes futuros: R$ {_fmt_brl(proj['gastos_recorrentes_futuros'])}\n"
        f"- Dias restantes no mês: {proj['dias_restantes']}\n\n"
        f"Investimentos:\n"
        f"- Total aportado: R$ {_fmt_brl(aportes)}\n"
        f"- Total resgatado: R$ {_fmt_brl(resgates)}\n"
        f"- Patrimônio investido líquido: R$ {_fmt_brl(patrimonio_investido)}\n"
        f"- Quantidade de lançamentos de investimento: {len(invs)}\n\n"
        f"Top categorias de gasto no mês:\n" + ("\n".join(top_lines) if top_lines else "- sem dados") + "\n\n"
        f"Alertas atuais:\n" + ("\n".join(alert_lines) if alert_lines else "- nenhum alerta importante") + "\n\n"
        f"Últimos lançamentos financeiros:\n" + ("\n".join(tx_lines) if tx_lines else "- sem lançamentos") + "\n\n"
        f"Últimos investimentos:\n" + ("\n".join(inv_lines) if inv_lines else "- sem investimentos")
    )


def _looks_like_finance_question(text_msg: str) -> bool:
    txt = _norm_word(text_msg)
    if not txt:
        return False
    keywords = {
        "gastei", "gasto", "gastos", "receita", "receitas", "saldo", "sobrou", "faltando",
        "projecao", "projeção", "alerta", "alertas", "investi", "investido",
        "investimentos", "patrimonio", "patrimônio", "aporte", "resgate", "mercado",
        "categoria", "categorias", "dinheiro", "financeiro", "financas", "finanças",
        "mes", "mês", "semana", "hoje", "quanto", "posso", "tenho"
    }
    return any(k in txt for k in keywords)


def _ask_openai_finance_assistant(user_id: int, question: str) -> str:
    if not _openai_available():
        return "⚠️ A IA ainda não está ativa no servidor. Configure OPENAI_API_KEY no Railway."

    context = _build_ai_finance_context(user_id)
    system = (
        "Você é o Finance AI, um assistente financeiro pessoal via WhatsApp. "
        "Responda sempre em português do Brasil, com linguagem clara, objetiva e amigável. "
        "Use SOMENTE o contexto financeiro fornecido. "
        "Se a pergunta fugir de finanças pessoais do usuário, diga que você ajuda apenas com dados financeiros do app. "
        "Não invente valores. Se algo não estiver no contexto, diga explicitamente que não encontrou dados suficientes. "
        "Quando fizer sentido, cite números exatos do contexto e dê uma dica prática curta no final. "
        "Mantenha a resposta curta, adequada para WhatsApp, no máximo 12 linhas."
    )
    user_prompt = (
        f"Contexto financeiro do usuário:\n{context}\n\n"
        f"Pergunta do usuário:\n{question.strip()}"
    )

    payload = {
        "model": OPENAI_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 350,
    }

    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers=_openai_headers(),
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    content = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    content = str(content).strip()
    return content or "Não consegui montar uma resposta agora. Tente novamente em instantes."


def _reply_finance_question(user_id: int, text_msg: str) -> str:
    try:
        return _ask_openai_finance_assistant(user_id, text_msg)
    except Exception as e:
        print("finance assistant error:", repr(e))
        return "Não consegui responder com a IA agora. Tente novamente em alguns instantes."


def _parse_kv_assignments(s: str) -> dict:
    out = {}
    pattern = re.compile(r'(\w+)\s*=\s*(".*?"|\'.*?\'|[^\s]+)')
    for m in pattern.finditer(s or ""):
        k = m.group(1).strip().lower()
        v = m.group(2).strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        out[k] = v
    return out


def _pending_get(wa_from: str):
    now = datetime.utcnow()
    WaPending.query.filter(WaPending.wa_from == wa_from, WaPending.expires_at < now).delete()
    db.session.commit()
    return (
        WaPending.query
        .filter(WaPending.wa_from == wa_from, WaPending.expires_at >= now)
        .order_by(WaPending.id.desc())
        .first()
    )


def _pending_set(wa_from: str, user_id: int, kind: str, payload: dict, minutes: int = 10):
    WaPending.query.filter_by(wa_from=wa_from, user_id=user_id).delete()
    db.session.commit()
    p = WaPending(
        wa_from=wa_from,
        user_id=user_id,
        kind=kind,
        payload_json=json.dumps(payload, ensure_ascii=False),
        expires_at=datetime.utcnow() + timedelta(minutes=minutes),
    )
    db.session.add(p)
    db.session.commit()


def _pending_clear(wa_from: str, user_id: int):
    WaPending.query.filter_by(wa_from=wa_from, user_id=user_id).delete()
    db.session.commit()


def _apply_edit_fields(tx: Transaction, fields: dict) -> tuple[bool, str]:
    if not fields:
        return False, "Nenhum campo informado."

    if "tipo" in fields:
        v = _norm_word(fields["tipo"])
        if v in ("receita", "gasto"):
            tx.tipo = "RECEITA" if v == "receita" else "GASTO"
        else:
            return False, "Tipo inválido. Use tipo=receita ou tipo=gasto"

    if "valor" in fields:
        try:
            tx.valor = _parse_brl_value(fields["valor"])
        except Exception:
            return False, "Valor inválido. Ex: valor=35,90"

    if "data" in fields:
        try:
            tx.data = _parse_date_any(fields["data"])
        except Exception:
            return False, "Data inválida. Ex: data=2026-03-01"

    if "categoria" in fields:
        cat = str(fields["categoria"] or "").strip()
        if not cat:
            return False, "Categoria vazia."
        tx.categoria = cat.title()

    if "descricao" in fields:
        desc = str(fields["descricao"] or "").strip()
        tx.descricao = desc or None

    return True, "OK"


def _next_monthly_date(from_date: date, day_of_month: int) -> date:
    y, m = from_date.year, from_date.month
    last_day = calendar.monthrange(y, m)[1]
    d = min(day_of_month, last_day)
    cand = date(y, m, d)
    if cand >= from_date:
        return cand

    if m == 12:
        y, m = y + 1, 1
    else:
        m += 1
    last_day = calendar.monthrange(y, m)[1]
    d = min(day_of_month, last_day)
    return date(y, m, d)


def _next_weekly_date(from_date: date, weekday: int) -> date:
    delta = (weekday - from_date.weekday()) % 7
    cand = from_date + timedelta(days=delta)
    if cand >= from_date:
        return cand
    return cand + timedelta(days=7)


def _parse_recorrente_args(rest: str):
    rest = (rest or "").strip()
    if not rest:
        return None
    return rest.split()


def _create_recurring_rule(user_id: int, freq_raw: str, parts: list[str]):
    freq = _norm_word(freq_raw)
    today = datetime.utcnow().date()

    if freq in ("mensal",):
        if len(parts) < 3:
            return None, "Use: recorrente mensal DIA VALOR CATEGORIA [descricao]"
        try:
            dom = int(parts[0])
        except Exception:
            return None, "Dia inválido. Ex: recorrente mensal 5 1200 aluguel"
        if dom < 1 or dom > 31:
            return None, "Dia do mês deve ser 1-31."

        try:
            valor = _parse_brl_value(parts[1])
        except Exception:
            return None, "Valor inválido."

        categoria = parts[2].title()
        descricao = " ".join(parts[3:]).strip() or None
        next_run = _next_monthly_date(today, dom)

        rule = RecurringRule(
            user_id=user_id,
            freq="MONTHLY",
            day_of_month=dom,
            weekday=None,
            tipo="GASTO",
            valor=valor,
            categoria=categoria,
            descricao=descricao,
            start_date=today,
            next_run=next_run,
        )
        return rule, None

    if freq in ("semanal",):
        if len(parts) < 3:
            return None, "Use: recorrente semanal SEG VALOR CATEGORIA [descricao]"
        wd = _norm_word(parts[0])
        if wd not in WEEKDAY_MAP:
            return None, "Dia da semana inválido. Use: seg/ter/qua/qui/sex/sab/dom"

        weekday = WEEKDAY_MAP[wd]
        try:
            valor = _parse_brl_value(parts[1])
        except Exception:
            return None, "Valor inválido."

        categoria = parts[2].title()
        descricao = " ".join(parts[3:]).strip() or None
        next_run = _next_weekly_date(today, weekday)

        rule = RecurringRule(
            user_id=user_id,
            freq="WEEKLY",
            day_of_month=None,
            weekday=weekday,
            tipo="GASTO",
            valor=valor,
            categoria=categoria,
            descricao=descricao,
            start_date=today,
            next_run=next_run,
        )
        return rule, None

    if freq in ("diario", "diário"):
        if len(parts) < 2:
            return None, "Use: recorrente diário VALOR CATEGORIA [descricao]"
        try:
            valor = _parse_brl_value(parts[0])
        except Exception:
            return None, "Valor inválido."

        categoria = parts[1].title()
        descricao = " ".join(parts[2:]).strip() or None

        rule = RecurringRule(
            user_id=user_id,
            freq="DAILY",
            day_of_month=None,
            weekday=None,
            tipo="GASTO",
            valor=valor,
            categoria=categoria,
            descricao=descricao,
            start_date=today,
            next_run=today,
        )
        return rule, None

    return None, "Frequência inválida. Use: diário | semanal | mensal"


def _run_recorrentes_for_user(user_id: int, today: date | None = None):
    today = today or datetime.utcnow().date()
    created = 0

    rules = (
        RecurringRule.query
        .filter(RecurringRule.user_id == user_id, RecurringRule.is_active == True)
        .order_by(RecurringRule.id.asc())
        .all()
    )

    for r in rules:
        while r.next_run <= today:
            tx = Transaction(
                user_id=user_id,
                tipo=r.tipo,
                data=r.next_run,
                categoria=r.categoria,
                descricao=r.descricao,
                valor=r.valor,
                origem="REC",
            )
            db.session.add(tx)
            created += 1

            if r.freq == "DAILY":
                r.next_run = r.next_run + timedelta(days=1)
            elif r.freq == "WEEKLY":
                r.next_run = r.next_run + timedelta(days=7)
            elif r.freq == "MONTHLY":
                base = r.next_run + timedelta(days=1)
                r.next_run = _next_monthly_date(base, int(r.day_of_month or 1))
            else:
                r.is_active = False
                break

    db.session.commit()
    return created


def _parse_wa_text(msg_text: str):
    t = (msg_text or "").strip()
    if not t:
        return {"cmd": "NONE"}

    if CMD_HELP_RE.match(t):
        return {"cmd": "HELP"}

    if CMD_DESFAZER_RE.match(t):
        return {"cmd": "DESFAZER"}

    if CMD_PROJECAO_RE.match(t):
        return {"cmd": "PROJECAO"}

    if CMD_ALERTAS_RE.match(t):
        return {"cmd": "ALERTAS"}

    m = CMD_RESUMO_RE.match(t)
    if m:
        return {"cmd": "RESUMO", "kind": m.group(1)}

    if CMD_SALDO_MES_RE.match(t):
        return {"cmd": "SALDO_MES"}

    m = CMD_ANALISE_RE.match(t)
    if m:
        return {"cmd": "ANALISE", "kind": m.group(2)}

    mset = CAT_SET_RE.match(t)
    if mset:
        key = mset.group(1).strip()
        cat = mset.group(2).strip()
        if not key or not cat:
            return {"cmd": "CAT_HELP"}
        return {"cmd": "CAT_SET", "key": key, "categoria": cat}

    mdel = CAT_DEL_RE.match(t)
    if mdel:
        key = mdel.group(1).strip()
        if not key:
            return {"cmd": "CAT_HELP"}
        return {"cmd": "CAT_DEL", "key": key}

    if CAT_LIST_RE.match(t):
        return {"cmd": "CAT_LIST"}

    m = REC_ADD_RE.match(t)
    if m:
        return {"cmd": "REC_ADD", "freq": m.group(1), "rest": m.group(2)}

    if REC_LIST_RE.match(t):
        return {"cmd": "REC_LIST"}

    m = REC_DEL_RE.match(t)
    if m:
        return {"cmd": "REC_DEL", "id": int(m.group(1))}

    if REC_RUN_RE.match(t):
        return {"cmd": "REC_RUN"}

    if CMD_ULTIMOS_RE.match(t):
        return {"cmd": "ULTIMOS"}

    m = CMD_APAGAR_RE.match(t)
    if m:
        return {"cmd": "APAGAR", "id": int(m.group(1))}

    m = CMD_CORRIGIR_ULTIMA_RE.match(t)
    if m:
        return {"cmd": "CORRIGIR_ULTIMA", "fields": _parse_kv_assignments(m.group(1))}

    m = CMD_EDITAR_RE.match(t)
    if m:
        return {"cmd": "EDITAR", "id": int(m.group(1)), "fields": _parse_kv_assignments(m.group(2))}

    low_simple = _norm_word(t)
    if low_simple in ("receita", "gasto"):
        return {"cmd": "CONFIRM_TIPO", "tipo": "RECEITA" if low_simple == "receita" else "GASTO"}

    low = _norm_word(t)
    low = re.sub(r"\s+", " ", low).strip()
    for alias in CONNECT_ALIASES:
        if low.startswith(_norm_word(alias) + " "):
            email = t.split(" ", 1)[1].strip()
            return {"cmd": "CONNECT", "email": _normalize_email(email)}

    m = VALUE_RE.search(low)
    if not m:
        return {"cmd": "NONE"}

    sign = m.group(1) or ""
    valor_raw = m.group(2)
    try:
        valor = _parse_brl_value(valor_raw)
    except Exception:
        return {"cmd": "NONE"}

    before = (low[: m.start()] or "").strip()
    after = (low[m.end():] or "").strip(" -–—")

    before_tokens = _tokenize(before)
    after_tokens = _tokenize(after)

    tipo, confidence = _detect_tipo_with_score(sign, before_tokens, after_tokens)

    categoria_fallback = "Outros"
    descricao = ""
    if after:
        parts = after.split(" ", 1)
        categoria_fallback = (parts[0] or "Outros").strip().title()
        descricao = parts[1].strip() if len(parts) > 1 else ""

    return {
        "cmd": "TX",
        "tipo": tipo,
        "tipo_confidence": confidence,
        "valor": valor,
        "categoria_fallback": categoria_fallback,
        "descricao": descricao,
        "data": datetime.utcnow().date(),
        "raw_text": t,
    }


# -------------------------
# WhatsApp Cloud API Webhook
# -------------------------
@app.get("/webhooks/whatsapp")
def wa_verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == WA_VERIFY_TOKEN:
        return challenge or "", 200
    return "forbidden", 403


@app.post("/webhooks/whatsapp")
def wa_webhook():
    payload = request.get_json(silent=True) or {}

    try:
        for entry in payload.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}
                for msg in value.get("messages", []) or []:
                    msg_type = msg.get("type")
                    if msg_type not in ("text", "audio", "image", "document"):
                        continue

                    msg_id = msg.get("id")
                    wa_from = _normalize_wa_number(msg.get("from") or "")
                    body = ((msg.get("text") or {}) or {}).get("body", "") or ""

                    if msg_id and ProcessedMessage.query.filter_by(msg_id=msg_id).first():
                        continue
                    if msg_id:
                        db.session.add(ProcessedMessage(msg_id=msg_id, wa_from=wa_from))
                        db.session.commit()

                    parsed = _parse_wa_text(body) if msg_type == "text" else {"cmd": "MEDIA", "media_type": msg_type}

                    if msg_type == "text" and parsed["cmd"] == "HELP":
                        wa_send_text(wa_from, _wa_help_text())
                        continue

                    if msg_type == "text" and parsed["cmd"] == "CONNECT":
                        email = parsed.get("email")
                        if not email or "@" not in email:
                            wa_send_text(wa_from, "Email inválido. Ex: conectar david@email.com")
                            continue

                        u = _get_or_create_user_by_email(email, password=None)

                        link = WaLink.query.filter_by(wa_from=wa_from).first()
                        already = False
                        if link:
                            already = (link.user_id == u.id)
                            link.user_id = u.id
                        else:
                            link = WaLink(wa_from=wa_from, user_id=u.id)
                            db.session.add(link)
                        db.session.commit()

                        wa_send_text(
                            wa_from,
                            (f"✅ {'Já estava' if already else 'WhatsApp'} conectado ao email: {email}\n\n"
                             "Digite 'ajuda' para ver os comandos.\n"
                             "Exemplo: paguei 32,90 mercado"),
                        )
                        continue

                    link = WaLink.query.filter_by(wa_from=wa_from).first()
                    if not link:
                        wa_send_text(
                            wa_from,
                            "🔒 Seu WhatsApp não está conectado.\n\nEnvie:\n"
                            "conectar SEU_EMAIL_DO_APP\n"
                            "Ex: conectar david@email.com\n\n"
                            "Depois digite: ajuda",
                        )
                        continue

                    if msg_type == "text" and _handle_pending_ai_confirmation(wa_from, link.user_id, body):
                        continue

                    if msg_type in ("audio", "image", "document"):
                        if _handle_whatsapp_media_message(link, wa_from, msg):
                            continue

                    if parsed["cmd"] == "DESFAZER":
                        limit_dt = datetime.utcnow() - timedelta(minutes=5)
                        last = (
                            Transaction.query
                            .filter(Transaction.user_id == link.user_id, Transaction.origem == "WA")
                            .order_by(Transaction.id.desc())
                            .first()
                        )
                        if not last:
                            wa_send_text(wa_from, "Não achei nenhum lançamento recente do WhatsApp para desfazer.")
                            continue
                        if last.created_at and last.created_at < limit_dt:
                            wa_send_text(wa_from, "Janela de segurança passou (5 min). Use 'ultimos' e 'apagar ID'.")
                            continue
                        txid = last.id
                        db.session.delete(last)
                        db.session.commit()
                        wa_send_text(wa_from, f"✅ Desfeito: ID {txid}")
                        continue

                    if parsed["cmd"] == "RESUMO":
                        wa_send_text(wa_from, _make_resumo_text(link.user_id, parsed.get("kind") or "mes"))
                        continue
                    if parsed["cmd"] == "SALDO_MES":
                        wa_send_text(wa_from, _make_resumo_text(link.user_id, "mes"))
                        continue

                    if parsed["cmd"] == "ANALISE":
                        wa_send_text(wa_from, _make_analise_text(link.user_id, parsed.get("kind")))
                        continue

                    if parsed["cmd"] == "PROJECAO":
                        wa_send_text(wa_from, _make_projection_text(link.user_id))
                        continue

                    if parsed["cmd"] == "ALERTAS":
                        wa_send_text(wa_from, _make_alerts_text(link.user_id))
                        continue

                    if parsed["cmd"] == "CONFIRM_TIPO":
                        pending = _pending_get(wa_from)
                        if not pending or pending.user_id != link.user_id:
                            wa_send_text(wa_from, "Não tenho nenhuma dúvida pendente agora. Digite 'ajuda'.")
                            continue
                        if pending.kind != "CONFIRM_TIPO":
                            wa_send_text(wa_from, "Pendência não reconhecida. Digite 'ajuda'.")
                            continue

                        payload_tx = json.loads(pending.payload_json)
                        payload_tx["tipo"] = parsed["tipo"]

                        guessed = _guess_category_from_text(link.user_id, payload_tx.get("raw_text", ""))
                        categoria = guessed or payload_tx.get("categoria_fallback") or "Outros"

                        ttx = Transaction(
                            user_id=link.user_id,
                            tipo=payload_tx["tipo"],
                            data=_parse_date_any(payload_tx.get("data")),
                            categoria=categoria,
                            descricao=(payload_tx.get("descricao") or None),
                            valor=_parse_brl_value(payload_tx.get("valor")),
                            origem="WA",
                        )
                        db.session.add(ttx)
                        db.session.commit()
                        _pending_clear(wa_from, link.user_id)

                        wa_send_text(
                            wa_from,
                            "✅ Lançamento salvo (confirmado)!\n"
                            f"ID: {ttx.id}\n"
                            f"Tipo: {ttx.tipo}\n"
                            f"Valor: R$ {_fmt_brl(ttx.valor)}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}",
                        )
                        continue

                    if parsed["cmd"] == "CAT_HELP":
                        wa_send_text(
                            wa_from,
                            "Use assim:\n"
                            "• categoria ifood = Alimentação\n"
                            "• remover categoria ifood\n"
                            "• categorias",
                        )
                        continue

                    if parsed["cmd"] == "CAT_SET":
                        key = (parsed.get("key") or "").strip()
                        cat = (parsed.get("categoria") or "").strip()
                        if not key or not cat:
                            wa_send_text(wa_from, "Formato inválido. Ex: categoria ifood = Alimentação")
                            continue

                        key_norm = _norm_word(key)
                        if len(key_norm) < 2:
                            wa_send_text(wa_from, "Chave muito curta. Ex: categoria uber = Transporte")
                            continue

                        existing = CategoryRule.query.filter_by(user_id=link.user_id, pattern=key_norm).first()
                        if existing:
                            existing.categoria = cat.title()
                            existing.priority = 10
                        else:
                            db.session.add(CategoryRule(user_id=link.user_id, pattern=key_norm, categoria=cat.title(), priority=10))
                        db.session.commit()

                        wa_send_text(wa_from, f"✅ Regra salva: '{key_norm}' => {cat.title()}")
                        continue

                    if parsed["cmd"] == "CAT_DEL":
                        key = _norm_word(parsed.get("key") or "")
                        if not key:
                            wa_send_text(wa_from, "Formato inválido. Ex: remover categoria uber")
                            continue
                        q = CategoryRule.query.filter_by(user_id=link.user_id, pattern=key)
                        deleted = q.delete()
                        db.session.commit()
                        wa_send_text(wa_from, "✅ Regra removida." if deleted else "ℹ️ Essa regra não existia.")
                        continue

                    if parsed["cmd"] == "CAT_LIST":
                        rules = (
                            CategoryRule.query
                            .filter_by(user_id=link.user_id)
                            .order_by(CategoryRule.priority.desc(), CategoryRule.id.desc())
                            .limit(30)
                            .all()
                        )
                        if not rules:
                            wa_send_text(wa_from,
                                "Você ainda não criou regras.\n\n"
                                "Exemplos:\n"
                                "• categoria ifood = Alimentação\n"
                                "• categoria uber = Transporte\n\n"
                                "Dica: o bot também tem categorias automáticas padrão.",
                            )
                        else:
                            lines = ["✅ Suas regras (até 30):"]
                            for r in rules:
                                lines.append(f"• {r.pattern} => {r.categoria}")
                            wa_send_text(wa_from, "\n".join(lines))
                        continue

                    if parsed["cmd"] == "REC_ADD":
                        parts = _parse_recorrente_args(parsed.get("rest") or "")
                        if not parts:
                            wa_send_text(wa_from, "Use: recorrente mensal 5 1200 aluguel")
                            continue

                        rule, err = _create_recurring_rule(link.user_id, parsed.get("freq") or "", parts)
                        if err:
                            wa_send_text(wa_from, "❌ " + err)
                            continue

                        db.session.add(rule)
                        db.session.commit()
                        wa_send_text(wa_from,
                            "✅ Recorrente criada!\n"
                            f"ID: {rule.id}\n"
                            f"Freq: {rule.freq}\n"
                            f"Próximo: {rule.next_run.isoformat()}\n"
                            f"Valor: R$ {_fmt_brl(rule.valor)}\n"
                            f"Categoria: {rule.categoria}"
                        )
                        continue

                    if parsed["cmd"] == "REC_LIST":
                        rules = (RecurringRule.query.filter_by(user_id=link.user_id).order_by(RecurringRule.id.desc()).limit(30).all())
                        if not rules:
                            wa_send_text(wa_from, "Você ainda não tem recorrentes. Ex: recorrente mensal 5 1200 aluguel")
                        else:
                            lines = ["🔁 Suas recorrentes (até 30):"]
                            for r in rules:
                                extra = ""
                                if r.freq == "MONTHLY":
                                    extra = f"dia {r.day_of_month}"
                                elif r.freq == "WEEKLY":
                                    inv = {v: k for k, v in WEEKDAY_MAP.items()}
                                    extra = f"{inv.get(r.weekday, 'dia')}"
                                lines.append(f"• ID {r.id} | {r.freq} {extra} | R$ {_fmt_brl(r.valor)} | {r.categoria} | próximo {r.next_run.isoformat()}")
                            lines.append("\nPara remover: remover recorrente ID")
                            lines.append("Para gerar agora: rodar recorrentes")
                            wa_send_text(wa_from, "\n".join(lines))
                        continue

                    if parsed["cmd"] == "REC_DEL":
                        rid = parsed["id"]
                        r = RecurringRule.query.filter_by(id=rid, user_id=link.user_id).first()
                        if not r:
                            wa_send_text(wa_from, "Não achei essa recorrente (ou não é sua). Use: recorrentes")
                            continue
                        db.session.delete(r)
                        db.session.commit()
                        wa_send_text(wa_from, f"✅ Recorrente removida: ID {rid}")
                        continue

                    if parsed["cmd"] == "REC_RUN":
                        created = _run_recorrentes_for_user(link.user_id)
                        wa_send_text(wa_from, f"✅ Recorrentes geradas: {created} lançamento(s).")
                        continue

                    if parsed["cmd"] == "ULTIMOS":
                        txs = (Transaction.query.filter(Transaction.user_id == link.user_id).order_by(Transaction.id.desc()).limit(5).all())
                        if not txs:
                            wa_send_text(wa_from, "Você ainda não tem lançamentos.")
                        else:
                            lines = ["🧾 Últimos 5 lançamentos:"]
                            for ttx in txs:
                                lines.append(f"• ID {ttx.id} | {ttx.tipo} | R$ {_fmt_brl(ttx.valor)} | {ttx.categoria} | {ttx.data.isoformat()}")
                            lines.append("\nPara editar: editar ID valor=... categoria=... data=... tipo=receita/gasto")
                            lines.append("Para apagar: apagar ID")
                            wa_send_text(wa_from, "\n".join(lines))
                        continue

                    if parsed["cmd"] == "APAGAR":
                        txid = parsed["id"]
                        ttx = Transaction.query.filter_by(id=txid, user_id=link.user_id).first()
                        if not ttx:
                            wa_send_text(wa_from, "Não achei esse ID (ou não é seu). Use: ultimos")
                            continue
                        db.session.delete(ttx)
                        db.session.commit()
                        wa_send_text(wa_from, f"✅ Apagado: ID {txid}")
                        continue

                    if parsed["cmd"] == "EDITAR":
                        txid = parsed["id"]
                        fields = parsed.get("fields") or {}
                        ttx = Transaction.query.filter_by(id=txid, user_id=link.user_id).first()
                        if not ttx:
                            wa_send_text(wa_from, "Não achei esse ID (ou não é seu). Use: ultimos")
                            continue

                        ok, msg2 = _apply_edit_fields(ttx, fields)
                        if not ok:
                            wa_send_text(wa_from, f"❌ Não consegui editar: {msg2}")
                            continue

                        db.session.commit()
                        wa_send_text(wa_from,
                            "✅ Editado!\n"
                            f"ID: {ttx.id}\n"
                            f"Tipo: {ttx.tipo}\n"
                            f"Valor: R$ {_fmt_brl(ttx.valor)}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}",
                        )
                        continue

                    if parsed["cmd"] == "CORRIGIR_ULTIMA":
                        fields = parsed.get("fields") or {}
                        ttx = Transaction.query.filter(Transaction.user_id == link.user_id).order_by(Transaction.id.desc()).first()
                        if not ttx:
                            wa_send_text(wa_from, "Você ainda não tem lançamentos.")
                            continue

                        ok, msg2 = _apply_edit_fields(ttx, fields)
                        if not ok:
                            wa_send_text(wa_from, f"❌ Não consegui corrigir: {msg2}")
                            continue

                        db.session.commit()
                        wa_send_text(wa_from,
                            "✅ Corrigido na última transação!\n"
                            f"ID: {ttx.id}\n"
                            f"Tipo: {ttx.tipo}\n"
                            f"Valor: R$ {_fmt_brl(ttx.valor)}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}",
                        )
                        continue

                    if parsed["cmd"] == "TX":
                        raw_text = parsed.get("raw_text") or ""
                        guessed = _guess_category_from_text(link.user_id, raw_text)
                        categoria = guessed or parsed.get("categoria_fallback") or "Outros"

                        if parsed.get("tipo_confidence") == "low":
                            _pending_set(
                                wa_from=wa_from,
                                user_id=link.user_id,
                                kind="CONFIRM_TIPO",
                                payload={
                                    "tipo": parsed["tipo"],
                                    "valor": str(parsed["valor"]),
                                    "categoria_fallback": parsed.get("categoria_fallback"),
                                    "descricao": parsed.get("descricao") or "",
                                    "data": parsed.get("data").isoformat() if isinstance(parsed.get("data"), date) else None,
                                    "raw_text": raw_text,
                                },
                                minutes=10,
                            )
                            wa_send_text(
                                wa_from,
                                "🤔 Fiquei em dúvida se isso foi *RECEITA* ou *GASTO*.\n\n"
                                f"Mensagem: {raw_text}\n"
                                f"Valor: R$ {_fmt_brl(parsed['valor'])}\n"
                                f"Categoria sugerida: {categoria}\n\n"
                                "Responda apenas com:\n"
                                "• receita\n"
                                "ou\n"
                                "• gasto",
                            )
                            continue

                        ttx = Transaction(
                            user_id=link.user_id,
                            tipo=parsed["tipo"],
                            data=parsed["data"],
                            categoria=categoria,
                            descricao=(parsed.get("descricao") or None),
                            valor=parsed["valor"],
                            origem="WA",
                        )
                        db.session.add(ttx)
                        db.session.commit()

                        wa_send_text(
                            wa_from,
                            "✅ Lançamento salvo!\n"
                            f"ID: {ttx.id}\n"
                            f"Tipo: {ttx.tipo}\n"
                            f"Valor: R$ {_fmt_brl(ttx.valor)}\n"
                            f"Categoria: {ttx.categoria}\n"
                            f"Data: {ttx.data.isoformat()}\n\n"
                            "Dica: digite 'ultimos' para ver e editar.",
                        )
                        continue

                    if msg_type == "text" and _looks_like_finance_question(body):
                        wa_send_text(wa_from, _reply_finance_question(link.user_id, body))
                        continue

                    wa_send_text(wa_from, "Não entendi. Digite: ajuda")

    except Exception as e:
        print("WA webhook error:", repr(e))

    return "ok", 200


@app.route("/api/score_financeiro")
def api_score_financeiro():
    uid = _require_login()
    if not uid:
        return jsonify({"error": "Não logado"}), 401

    q = (
        Transaction.query
        .filter(Transaction.user_id == uid)
        .all()
    )

    receitas = sum(Decimal(t.valor or 0) for t in q if (t.tipo or "").upper() == "RECEITA")
    gastos = sum(Decimal(t.valor or 0) for t in q if (t.tipo or "").upper() == "GASTO")
    saldo = receitas - gastos

    score = 50
    status = "atencao"

    if receitas > 0:
        ratio = gastos / receitas
        if ratio < Decimal("0.50"):
            score = 90
            status = "saudavel"
        elif ratio < Decimal("0.70"):
            score = 80
            status = "saudavel"
        elif ratio < Decimal("0.90"):
            score = 65
            status = "atencao"
        else:
            score = 40
            status = "critico"
    elif gastos > 0:
        score = 25
        status = "critico"

    if saldo > 0 and score < 100:
        score = min(100, score + 5)

    return jsonify({
        "score": int(score),
        "status": status,
        "receitas": float(receitas),
        "gastos": float(gastos),
        "saldo": float(saldo)
    })


# -------------------------
# Entry
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
