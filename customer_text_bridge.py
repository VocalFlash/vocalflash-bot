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


def build_owner_whatsapp_response(api_result):
    """Create a deterministic, non-sensitive acknowledgement for owner commands.

    Never echo model-generated prose or internal context to WhatsApp. The
    Assistant API's structured owner result is the sole source of the status.
    """
    if not isinstance(api_result, dict) or api_result.get("ok") is not True:
        raise ValueError("Risultato comando titolare non valido")
    if api_result.get("origin") != "owner":
        raise ValueError("Origine messaggio non titolare")

    dispatch = api_result.get("result")
    owner = dispatch.get("owner") if isinstance(dispatch, dict) else None
    if not isinstance(owner, dict):
        raise ValueError("Esito comando titolare assente")

    status = clean_text(owner.get("status")).upper()
    effect_applied = owner.get("effect_applied") is True
    writes_performed = owner.get("writes_performed") is True

    if status == "CONFIRMATION_REQUIRED":
        return (
            "Ho preparato la proposta. Non è stata ancora applicata alcuna modifica. "
            "Per autorizzarla, rispondi esattamente: confermo."
        )
    if status == "CONFIRMATION_COMMITTED" and effect_applied:
        return "Conferma verificata: operazione completata."
    if status == "NO_PENDING_PROPOSAL":
        return "Non risulta alcuna proposta in attesa di conferma. Nessuna modifica eseguita."
    if status == "AMBIGUOUS_PENDING_PROPOSAL":
        return (
            "Ci sono più proposte compatibili. Per sicurezza non ho eseguito modifiche; "
            "specifica quale proposta intendi confermare."
        )
    if status == "READ_ONLY_READY":
        return "Verifica completata in sola lettura. Nessuna modifica effettuata."
    if status == "CONFIG_REQUIRED":
        return "La funzione non è attualmente configurata. Nessuna modifica è stata applicata."
    if effect_applied and writes_performed:
        return "Operazione completata e registrata."
    if status in {"DENIED", "MEMBER_NOT_ACTIVE", "CHANNEL_NOT_ACTIVE"}:
        return "Comando non autorizzato per questo account o canale. Nessuna operazione eseguita."
    if status in {"REPLAY_CONFLICT", "EXPIRED", "PROPOSAL_EXPIRED"}:
        return (
            "La richiesta non può essere applicata perché non supera i controlli di coerenza "
            "o validità. Nessuna nuova operazione è stata eseguita."
        )

    # Do not guess whether a side effect occurred for an unknown status.
    return (
        "Ho ricevuto il comando, ma non posso confernarne l'esito in modo sicuro. "
        "Non inviare nuovamente il comando: è necessaria una verifica."
    )


def build_owner_whatsapp_response(api_result):
    """Return a deterministic acknowledgement for a verified owner command."""
    if not isinstance(api_result, dict) or api_result.get("ok") is not True:
        raise ValueError("OWNER_RESULT_INVALID")
    if api_result.get("origin") != "owner":
        raise ValueError("OWNER_ORIGIN_REQUIRED")
    dispatch = api_result.get("result")
    owner = dispatch.get("owner") if isinstance(dispatch, dict) else None
    if not isinstance(owner, dict):
        raise ValueError("OWNER_OUTCOME_MISSING")

    status = clean_text(owner.get("status")).upper()
    effect_applied = owner.get("effect_applied") is True
    writes_performed = owner.get("writes_performed") is True

    if status == "CONFIRMATION_REQUIRED":
        return "Ho preparato la proposta. Non è stata ancora applicata. Per autorizzarla, rispondi esattamente: confermo."
    if status == "CONFIRMATION_COMMITTED" and effect_applied:
        return "Conferma verificata: operazione completata."
    if status == "NO_PENDING_PROPOSAL":
        return "Non risulta alcuna proposta in attesa di conferma. Nessuna modifica eseguita."
    if status == "AMBIGUOUS_PENDING_PROPOSAL":
        return "Ci sono più proposte compatibili. Per sicurezza non ho eseguito modifiche; specifica quale intendi confermare."
    if status == "READ_ONLY_READY":
        return "Verifica completata in sola lettura. Nessuna modifica effettuata."
    if status == "CONFIG_REQUIRED":
        return "La funzione non è attualmente configurata. Nessuna modifica è stata applicata."
    if effect_applied and writes_performed:
        return "Operazione completata e registrata."
    if status in {"DENIED", "MEMBER_NOT_ACTIVE", "CHANNEL_NOT_ACTIVE"}:
        return "Comando non autorizzato per questo account o canale. Nessuna operazione eseguita."
    if status in {"REPLAY_CONFLICT", "EXPIRED", "PROPOSAL_EXPIRED"}:
        return "La richiesta non supera i controlli di coerenza o validità. Nessuna nuova operazione è stata eseguita."
    return "Ho ricevuto il comando, ma non posso confermarne l'esito in modo sicuro. Non ripeterlo: è necessaria una verifica."
