
# -*- coding: utf-8 -*-
import json
from datetime import datetime, date, timedelta

from flask import request, jsonify

from whatsapp_commands import WEEKDAY_MAP


def register_whatsapp_routes(
    app,
    db,
    User,
    Transaction,
    Investment,
    WaLink,
    ProcessedMessage,
    CategoryRule,
    WaPending,
    RecurringRule,
    WA_VERIFY_TOKEN,
    parse_wa_text,
    wa_help_text,
    normalize_wa_number,
    get_or_create_user_by_email,
    wa_send_text,
    handle_pending_ai_confirmation,
    handle_whatsapp_media_message,
    make_resumo_text,
    make_analise_text,
    make_projection_text,
    make_alerts_text,
    guess_category_from_text,
    parse_date_any,
    parse_brl_value,
    fmt_brl,
    norm_word,
    pending_get,
    pending_set,
    pending_clear,
    parse_recorrente_args,
    create_recurring_rule,
    run_recorrentes_for_user,
    apply_edit_fields,
    looks_like_finance_question,
    reply_finance_question,
):
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
                        wa_from = normalize_wa_number(msg.get("from") or "")
                        body = ((msg.get("text") or {}) or {}).get("body", "") or ""

                        if msg_id and ProcessedMessage.query.filter_by(msg_id=msg_id).first():
                            continue
                        if msg_id:
                            db.session.add(ProcessedMessage(msg_id=msg_id, wa_from=wa_from))
                            db.session.commit()

                        parsed = parse_wa_text(body) if msg_type == "text" else {"cmd": "MEDIA", "media_type": msg_type}

                        if msg_type == "text" and parsed["cmd"] == "HELP":
                            wa_send_text(wa_from, wa_help_text())
                            continue

                        if msg_type == "text" and parsed["cmd"] == "CONNECT":
                            email = parsed.get("email")
                            if not email or "@" not in email:
                                wa_send_text(wa_from, "Email inválido. Ex: conectar david@email.com")
                                continue

                            u = get_or_create_user_by_email(User, db, email, password=None)

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
                                (
                                    f"✅ {'Já estava' if already else 'WhatsApp'} conectado ao email: {email}\n\n"
                                    "Digite 'ajuda' para ver os comandos.\n"
                                    "Exemplo: paguei 32,90 mercado"
                                ),
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

                        if msg_type == "text" and handle_pending_ai_confirmation(wa_from, link.user_id, body):
                            continue

                        if msg_type in ("audio", "image", "document"):
                            if handle_whatsapp_media_message(link, wa_from, msg):
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
                            wa_send_text(wa_from, make_resumo_text(link.user_id, parsed.get("kind") or "mes"))
                            continue

                        if parsed["cmd"] == "SALDO_MES":
                            wa_send_text(wa_from, make_resumo_text(link.user_id, "mes"))
                            continue

                        if parsed["cmd"] == "ANALISE":
                            wa_send_text(wa_from, make_analise_text(link.user_id, parsed.get("kind")))
                            continue

                        if parsed["cmd"] == "PROJECAO":
                            wa_send_text(wa_from, make_projection_text(link.user_id))
                            continue

                        if parsed["cmd"] == "ALERTAS":
                            wa_send_text(wa_from, make_alerts_text(link.user_id))
                            continue

                        if parsed["cmd"] == "CONFIRM_TIPO":
                            pending = pending_get(wa_from)
                            if not pending or pending.user_id != link.user_id:
                                wa_send_text(wa_from, "Não tenho nenhuma dúvida pendente agora. Digite 'ajuda'.")
                                continue
                            if pending.kind != "CONFIRM_TIPO":
                                wa_send_text(wa_from, "Pendência não reconhecida. Digite 'ajuda'.")
                                continue

                            payload_tx = json.loads(pending.payload_json)
                            payload_tx["tipo"] = parsed["tipo"]

                            guessed = guess_category_from_text(link.user_id, payload_tx.get("raw_text", ""))
                            categoria = guessed or payload_tx.get("categoria_fallback") or "Outros"

                            ttx = Transaction(
                                user_id=link.user_id,
                                tipo=payload_tx["tipo"],
                                data=parse_date_any(payload_tx.get("data")),
                                categoria=categoria,
                                descricao=(payload_tx.get("descricao") or None),
                                valor=parse_brl_value(payload_tx.get("valor")),
                                origem="WA",
                            )
                            db.session.add(ttx)
                            db.session.commit()
                            pending_clear(wa_from, link.user_id)

                            wa_send_text(
                                wa_from,
                                "✅ Lançamento salvo (confirmado)!\n"
                                f"ID: {ttx.id}\n"
                                f"Tipo: {ttx.tipo}\n"
                                f"Valor: R$ {fmt_brl(ttx.valor)}\n"
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

                            key_norm = norm_word(key)
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
                            key = norm_word(parsed.get("key") or "")
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
                                wa_send_text(
                                    wa_from,
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
                            parts = parse_recorrente_args(parsed.get("rest") or "")
                            if not parts:
                                wa_send_text(wa_from, "Use: recorrente mensal 5 1200 aluguel")
                                continue

                            rule, err = create_recurring_rule(link.user_id, parsed.get("freq") or "", parts)
                            if err:
                                wa_send_text(wa_from, "❌ " + err)
                                continue

                            db.session.add(rule)
                            db.session.commit()
                            wa_send_text(
                                wa_from,
                                "✅ Recorrente criada!\n"
                                f"ID: {rule.id}\n"
                                f"Freq: {rule.freq}\n"
                                f"Próximo: {rule.next_run.isoformat()}\n"
                                f"Valor: R$ {fmt_brl(rule.valor)}\n"
                                f"Categoria: {rule.categoria}",
                            )
                            continue

                        if parsed["cmd"] == "REC_LIST":
                            rules = RecurringRule.query.filter_by(user_id=link.user_id).order_by(RecurringRule.id.desc()).limit(30).all()
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
                                    lines.append(
                                        f"• ID {r.id} | {r.freq} {extra} | R$ {fmt_brl(r.valor)} | {r.categoria} | próximo {r.next_run.isoformat()}"
                                    )
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
                            created = run_recorrentes_for_user(link.user_id)
                            wa_send_text(wa_from, f"✅ Recorrentes geradas: {created} lançamento(s).")
                            continue

                        if parsed["cmd"] == "ULTIMOS":
                            txs = Transaction.query.filter(Transaction.user_id == link.user_id).order_by(Transaction.id.desc()).limit(5).all()
                            if not txs:
                                wa_send_text(wa_from, "Você ainda não tem lançamentos.")
                            else:
                                lines = ["🧾 Últimos 5 lançamentos:"]
                                for ttx in txs:
                                    lines.append(f"• ID {ttx.id} | {ttx.tipo} | R$ {fmt_brl(ttx.valor)} | {ttx.categoria} | {ttx.data.isoformat()}")
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

                            ok, msg2 = apply_edit_fields(ttx, fields)
                            if not ok:
                                wa_send_text(wa_from, f"❌ Não consegui editar: {msg2}")
                                continue

                            db.session.commit()
                            wa_send_text(
                                wa_from,
                                "✅ Editado!\n"
                                f"ID: {ttx.id}\n"
                                f"Tipo: {ttx.tipo}\n"
                                f"Valor: R$ {fmt_brl(ttx.valor)}\n"
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

                            ok, msg2 = apply_edit_fields(ttx, fields)
                            if not ok:
                                wa_send_text(wa_from, f"❌ Não consegui corrigir: {msg2}")
                                continue

                            db.session.commit()
                            wa_send_text(
                                wa_from,
                                "✅ Corrigido na última transação!\n"
                                f"ID: {ttx.id}\n"
                                f"Tipo: {ttx.tipo}\n"
                                f"Valor: R$ {fmt_brl(ttx.valor)}\n"
                                f"Categoria: {ttx.categoria}\n"
                                f"Data: {ttx.data.isoformat()}",
                            )
                            continue

                        if parsed["cmd"] == "TX":
                            raw_text = parsed.get("raw_text") or ""
                            guessed = guess_category_from_text(link.user_id, raw_text)
                            categoria = guessed or parsed.get("categoria_fallback") or "Outros"

                            if parsed.get("tipo_confidence") == "low":
                                pending_set(
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
                                    f"Valor: R$ {fmt_brl(parsed['valor'])}\n"
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
                                f"Valor: R$ {fmt_brl(ttx.valor)}\n"
                                f"Categoria: {ttx.categoria}\n"
                                f"Data: {ttx.data.isoformat()}\n\n"
                                "Dica: digite 'ultimos' para ver e editar.",
                            )
                            continue

                        if msg_type == "text" and looks_like_finance_question(body):
                            wa_send_text(wa_from, reply_finance_question(link.user_id, body))
                            continue

                        wa_send_text(wa_from, "Não entendi. Digite: ajuda")

        except Exception as e:
            print("WA webhook error:", repr(e))

        return "ok", 200
