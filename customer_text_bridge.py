import os
from datetime import datetime, timezone

import requests


ASSISTANT_TIMEOUT = (10, 120)

MULTIVOCALE_TEXT_COMMANDS = {
    "vf_multi",
    "multi",
    "riepiloga più vocali",
    "vf_cancel",
    "annulla",
    "vf_retry",
    "vf_more",
    "vf_finish",
    "riepiloga",
}


def clean_text(value):
    return str(value or "").strip()


def is_multivocale_text_command(value):
    return clean_text(value).lower() in MULTIVOCALE_TEXT_COMMANDS


def message_timestamp_iso(message):
    raw = clean_text((message or {}).get("timestamp"))
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(
            int(raw),
            tz=timezone.utc,
        ).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def get_assistant_url():
    return clean_text(
        os.getenv("VOCALFLASH_ASSISTANT_API_URL", "")
    ).rstrip("/")


def get_assistant_api_key():
    return clean_text(
        os.getenv("VOCALFLASH_ASSISTANT_API_KEY", "")
    )


def get_vercel_bypass_secret():
    return clean_text(
        os.getenv("VERCEL_AUTOMATION_BYPASS_SECRET", "")
    )


def internal_headers(api_key, vercel_bypass_secret=None):
    headers = {
        "X-VocalFlash-Assistant-Key": clean_text(api_key),
        "Content-Type": "application/json",
    }
    bypass = clean_text(
        vercel_bypass_secret
        if vercel_bypass_secret is not None
        else get_vercel_bypass_secret()
    )
    if bypass:
        headers["x-vercel-protection-bypass"] = bypass
    return headers


def forward_customer_text(
    message,
    config,
    external_account_id=None,
    assistant_url=None,
    assistant_api_key=None,
    vercel_bypass_secret=None,
    requests_module=requests,
):
    """Forward one ordinary customer text to the central Assistant API.

    This function does not send a WhatsApp reply and does not handle owner
    commands. It only hands off the customer message to the server-side
    ingest/routing pipeline. Assistant authentication is intentionally
    separate from the transcription API credential.
    """
    if not isinstance(message, dict):
        raise ValueError("Messaggio WhatsApp non valido")

    sender = clean_text(message.get("from"))
    message_id = clean_text(message.get("id"))
    body = clean_text((message.get("text") or {}).get("body"))
    account_id = clean_text(
        external_account_id or config.get("phone_id")
    )
    api_key = clean_text(
        assistant_api_key or get_assistant_api_key()
    )
    url = clean_text(assistant_url or get_assistant_url())

    if not url:
        return {
            "ok": False,
            "status": "DISABLED",
            "reason": "ASSISTANT_URL_NOT_CONFIGURED",
        }

    if not api_key:
        raise RuntimeError("ASSISTANT_API_KEY_NOT_CONFIGURED")

    if not sender or not message_id or not body or not account_id:
        raise ValueError("Dati customer assistant incompleti")

    response = requests_module.post(
        url,
        headers=internal_headers(api_key, vercel_bypass_secret),
        json={
            "external_account_id": account_id,
            "sender_wa_id": sender,
            "external_message_id": message_id,
            "normalized_text": body,
            "occurred_at": message_timestamp_iso(message),
            "content_type": "text",
        },
        timeout=ASSISTANT_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    if not isinstance(data, dict) or data.get("ok") is not True:
        raise ValueError("Risposta Customer Assistant non valida")

    return data
