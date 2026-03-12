# -*- coding: utf-8 -*-
import json
import os
import re
from datetime import datetime, timedelta, date

from finance_services import WEEKDAY_MAP
from utils_core import (
    fmt_brl,
    next_monthly_date,
    next_weekly_date,
    norm_word,
    parse_brl_value,
    parse_date_any,
)
from utils_integrations import (
    _analyze_image_transaction,
    _analyze_text_transaction,
    _download_whatsapp_media,
    _extract_pdf_text,
    _openai_available,
    _transcribe_audio_file,
    wa_send_text,
)


def _get_runtime_objects():
    from app import db, Transaction, WaPending, WaLink, RecurringRule
    return db, Transaction, WaPending, WaLink, RecurringRule

def _save_ai_transaction(user_id: int, tx_data: dict, origem: str = "WA"):
    db, Transaction, WaPending, WaLink, RecurringRule = _get_runtime_objects()
    tx = Transaction(
        user_id=user_id,
        tipo=tx_data["tipo"],
        data=parse_date_any(tx_data.get("data")),
        categoria=(tx_data.get("categoria") or "Outros").title(),
        descricao=tx_data.get("descricao") or None,
        valor=parse_brl_value(tx_data.get("valor")),
        origem=origem,
    )
    db.session.add(tx)
    db.session.commit()
    return tx


def _pending_confirmation_choice(text_msg: str) -> str | None:
    norm = norm_word(text_msg)
    if norm in ("1", "sim", "s", "confirmar", "ok"):
        return "confirm"
    if norm in ("2", "nao", "não", "n", "cancelar", "cancela"):
        return "cancel"
    return None


def _pending_get(wa_from: str):
    db, Transaction, WaPending, WaLink, RecurringRule = _get_runtime_objects()
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
    db, Transaction, WaPending, WaLink, RecurringRule = _get_runtime_objects()
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
    db, Transaction, WaPending, WaLink, RecurringRule = _get_runtime_objects()
    WaPending.query.filter_by(wa_from=wa_from, user_id=user_id).delete()
    db.session.commit()


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
    wa_send_text(
        wa_from,
        "✅ Lançamento salvo!\n"
        f"ID: {tx.id}\n"
        f"Tipo: {tx.tipo}\n"
        f"Valor: R$ {fmt_brl(tx.valor)}\n"
        f"Categoria: {tx.categoria}\n"
        f"Data: {tx.data.isoformat()}"
    )
    return True


def _send_ai_confirmation_request(wa_from: str, user_id: int, tx_data: dict, source_label: str):
    _pending_set(wa_from, user_id, "CONFIRM_AI_TX", {"tx": tx_data, "source": source_label}, minutes=15)
    wa_send_text(
        wa_from,
        "🤖 Identifiquei este lançamento, mas quero sua confirmação:\n\n"
        f"Tipo: {tx_data['tipo']}\n"
        f"Valor: R$ {fmt_brl(tx_data['valor'])}\n"
        f"Categoria: {tx_data['categoria']}\n"
        f"Descrição: {tx_data.get('descricao') or '-'}\n"
        f"Data: {tx_data['data']}\n\n"
        "Responda com:\n"
        "1 = confirmar\n"
        "2 = cancelar"
    )


def _handle_whatsapp_media_message(link, wa_from: str, msg: dict) -> bool:
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
            wa_send_text(
                wa_from,
                "✅ Lançamento salvo pela IA!\n"
                f"ID: {tx.id}\n"
                f"Tipo: {tx.tipo}\n"
                f"Valor: R$ {fmt_brl(tx.valor)}\n"
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


def _apply_edit_fields(tx, fields: dict) -> tuple[bool, str]:
    if not fields:
        return False, "Nenhum campo informado."

    if "tipo" in fields:
        v = norm_word(fields["tipo"])
        if v in ("receita", "gasto"):
            tx.tipo = "RECEITA" if v == "receita" else "GASTO"
        else:
            return False, "Tipo inválido. Use tipo=receita ou tipo=gasto"

    if "valor" in fields:
        try:
            tx.valor = parse_brl_value(fields["valor"])
        except Exception:
            return False, "Valor inválido. Ex: valor=35,90"

    if "data" in fields:
        try:
            tx.data = parse_date_any(fields["data"])
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


def _parse_recorrente_args(rest: str):
    rest = (rest or "").strip()
    if not rest:
        return None
    return rest.split()


def _create_recurring_rule(user_id: int, freq_raw: str, parts: list[str]):
    db, Transaction, WaPending, WaLink, RecurringRule = _get_runtime_objects()
    freq = norm_word(freq_raw)
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
            valor = parse_brl_value(parts[1])
        except Exception:
            return None, "Valor inválido."

        categoria = parts[2].title()
        descricao = " ".join(parts[3:]).strip() or None
        next_run = next_monthly_date(today, dom)

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
        wd = norm_word(parts[0])
        if wd not in WEEKDAY_MAP:
            return None, "Dia da semana inválido. Use: seg/ter/qua/qui/sex/sab/dom"

        weekday = WEEKDAY_MAP[wd]
        try:
            valor = parse_brl_value(parts[1])
        except Exception:
            return None, "Valor inválido."

        categoria = parts[2].title()
        descricao = " ".join(parts[3:]).strip() or None
        next_run = next_weekly_date(today, weekday)

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
            valor = parse_brl_value(parts[0])
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
    db, Transaction, WaPending, WaLink, RecurringRule = _get_runtime_objects()
    today = today or datetime.utcnow().date()
    created = 0

    rules = (
        RecurringRule.query
        .filter(RecurringRule.user_id == user_id, RecurringRule.is_active.is_(True))
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
                r.next_run = next_monthly_date(base, int(r.day_of_month or 1))
            else:
                r.is_active = False
                break

    db.session.commit()
    return created

