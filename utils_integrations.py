# -*- coding: utf-8 -*-
import os
import base64
import tempfile

import requests
from PyPDF2 import PdfReader

from utils_core import normalize_wa_number, parse_brl_value, parse_date_any, extract_json_from_text

_CONFIG = {
    "wa_access_token": "",
    "wa_phone_number_id": "",
    "graph_version": "v20.0",
    "openai_api_key": "",
    "openai_chat_model": "gpt-4.1-mini",
    "openai_vision_model": "gpt-4.1-mini",
    "openai_transcribe_model": "gpt-4o-mini-transcribe",
}


def init_integrations(
    *,
    wa_access_token: str,
    wa_phone_number_id: str,
    graph_version: str,
    openai_api_key: str,
    openai_chat_model: str,
    openai_vision_model: str,
    openai_transcribe_model: str,
):
    _CONFIG.update({
        "wa_access_token": wa_access_token or "",
        "wa_phone_number_id": wa_phone_number_id or "",
        "graph_version": graph_version or "v20.0",
        "openai_api_key": openai_api_key or "",
        "openai_chat_model": openai_chat_model or "gpt-4.1-mini",
        "openai_vision_model": openai_vision_model or openai_chat_model or "gpt-4.1-mini",
        "openai_transcribe_model": openai_transcribe_model or "gpt-4o-mini-transcribe",
    })


def wa_send_text(to_number: str, text_msg: str):
    to_number = normalize_wa_number(to_number)
    if not (_CONFIG["wa_phone_number_id"] and _CONFIG["wa_access_token"] and to_number):
        print("WA send skipped (missing creds or number). msg:", text_msg)
        return

    url = f"https://graph.facebook.com/{_CONFIG['graph_version']}/{_CONFIG['wa_phone_number_id']}/messages"
    headers = {
        "Authorization": f"Bearer {_CONFIG['wa_access_token']}",
        "Content-Type": "application/json",
    }
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
        "Authorization": f"Bearer {_CONFIG['openai_api_key']}",
        "Content-Type": "application/json",
    }


def _openai_available() -> bool:
    return bool(_CONFIG["openai_api_key"])


def _download_whatsapp_media(media_id: str, fallback_name: str = "arquivo"):
    if not media_id or not _CONFIG["wa_access_token"]:
        raise ValueError("mídia indisponível")

    meta_url = f"https://graph.facebook.com/{_CONFIG['graph_version']}/{media_id}"
    headers = {"Authorization": f"Bearer {_CONFIG['wa_access_token']}"}
    r = requests.get(meta_url, headers=headers, timeout=20)
    r.raise_for_status()
    meta = r.json()

    dl_url = meta.get("url")
    mime_type = meta.get("mime_type") or "application/octet-stream"
    ext_map = {
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "image/jpeg": ".jpg",
        "image/png": ".png",
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
        data = {"model": _CONFIG["openai_transcribe_model"]}
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {_CONFIG['openai_api_key']}"},
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


def _normalize_ai_result(obj: dict) -> dict | None:
    if not obj:
        return None

    try:
        valor = parse_brl_value(obj.get("valor"))
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
        "data": parse_date_any(obj.get("data")).isoformat(),
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
        "Se houver pix enviado/pagamento/compra, normalmente é GASTO. "
        "Se houver pix recebido/recebi/depósito recebido, normalmente é RECEITA."
    )

    content = [{"type": "text", "text": user_prompt}]
    if image_base64 and mime_type:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{image_base64}"}
        })

    payload = {
        "model": _CONFIG["openai_vision_model"] if image_base64 else _CONFIG["openai_chat_model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content if image_base64 else user_prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers=_openai_headers(),
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"]
    return _normalize_ai_result(extract_json_from_text(raw))


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
