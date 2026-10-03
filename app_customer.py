import os

import app as legacy
from customer_text_bridge import (
    clean_text,
    forward_customer_text,
    is_multivocale_text_command,
)
from multivocale_runtime_v2 import PersistentMultivocaleRuntime


app = legacy.app
_original_handle_message = legacy.handle_message
_multivocale_runtime = PersistentMultivocaleRuntime(legacy)


def _release_text_dedup(message_id, redis_url):
    """Allow Meta to retry a text if the central Assistant call failed."""
    try:
        client = legacy.get_redis_client(redis_url)
        if client is not None:
            client.delete(f"vf:seen:{message_id}")
    except Exception as exc:
        legacy.log(
            "Rilascio dedup testo Redis non riuscito: "
            f"{type(exc).__name__}"
        )

    with legacy.state_condition:
        legacy.seen_messages.pop(message_id, None)
        legacy.state_condition.notify_all()


def _is_multivocale_candidate(message):
    if not isinstance(message, dict):
        return False
    message_type = message.get("type")
    if message_type in {"audio", "interactive"}:
        return True
    if message_type == "text":
        body = clean_text((message.get("text") or {}).get("body"))
        return is_multivocale_text_command(body)
    return False


def _handle_multivocale_v2(message, config):
    sender = clean_text(message.get("from"))
    message_id = clean_text(message.get("id"))
    if not sender or not message_id:
        return True

    legacy.cleanup_expired()
    if legacy.is_duplicate(message_id, config["redis_url"]):
        legacy.log("Messaggio duplicato ignorato")
        return True

    try:
        handled = _multivocale_runtime.handle_message(message, config)
        if handled:
            return True
    except Exception:
        _release_text_dedup(message_id, config["redis_url"])
        raise

    # An interactive message unrelated to MultiVocale should preserve the
    # legacy behavior. Release our preliminary dedup reservation first.
    _release_text_dedup(message_id, config["redis_url"])
    return False


def handle_message_with_customer_assistant(message, config):
    """Route MultiVocale to V2 and ordinary customer text to Assistant API.

    Other message types continue to use the established legacy behavior.
    Assistant authentication remains separate from the transcription API key.
    """
    if not isinstance(message, dict):
        return _original_handle_message(message, config)

    if _is_multivocale_candidate(message):
        if _handle_multivocale_v2(message, config):
            return

    message_type = message.get("type")
    if message_type != "text":
        return _original_handle_message(message, config)

    body = clean_text((message.get("text") or {}).get("body"))
    assistant_url = clean_text(
        os.getenv("VOCALFLASH_ASSISTANT_API_URL", "")
    )
    if not assistant_url:
        return _original_handle_message(message, config)

    sender = clean_text(message.get("from"))
    message_id = clean_text(message.get("id"))
    if not sender or not message_id or not body:
        return _original_handle_message(message, config)

    legacy.cleanup_expired()
    if legacy.is_duplicate(message_id, config["redis_url"]):
        legacy.log("Messaggio duplicato ignorato")
        return

    legacy.log("Messaggio customer text: inoltro Assistant API")

    try:
        result = forward_customer_text(
            message,
            config,
            external_account_id=config.get("phone_id"),
            assistant_url=assistant_url,
        )
        routing = result.get("routing") or {}
        legacy.log(
            "Customer Assistant completato: "
            f"replayed={routing.get('replayed')}"
        )
    except Exception:
        _release_text_dedup(
            message_id,
            config["redis_url"],
        )
        raise


legacy.handle_message = handle_message_with_customer_assistant
