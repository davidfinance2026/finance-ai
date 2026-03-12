# -*- coding: utf-8 -*-
import re
import json
import hashlib
import calendar
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation


def hash_password(pw: str) -> str:
    return hashlib.sha256((pw or "").encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def parse_brl_value(v) -> Decimal:
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


def parse_date_any(v) -> date:
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


def parse_money_br_to_decimal(value):
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


def iso_date(value):
    s = str(value or "").strip()
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return datetime.utcnow().date()


def extract_json_from_text(raw: str) -> dict:
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


def fmt_brl(v: Decimal | float | int | None) -> str:
    try:
        d = Decimal(v or 0)
    except Exception:
        d = Decimal("0")
    s = f"{d:.2f}"
    return s.replace(".", ",")


def month_bounds(year: int, month: int):
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def norm_word(w: str) -> str:
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


def tokenize(textv: str) -> list[str]:
    textv = norm_word(textv)
    parts = re.split(r"[^a-z0-9]+", textv)
    return [p for p in parts if p]


def normalize_wa_number(raw: str) -> str:
    s = (raw or "").strip().replace("+", "")
    s = re.sub(r"[^0-9]", "", s)
    return s


def period_range(kind: str):
    today = datetime.utcnow().date()
    k = norm_word(kind)
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


def next_monthly_date(from_date: date, day_of_month: int) -> date:
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


def next_weekly_date(from_date: date, weekday: int) -> date:
    delta = (weekday - from_date.weekday()) % 7
    cand = from_date + timedelta(days=delta)
    if cand >= from_date:
        return cand
    return cand + timedelta(days=7)
