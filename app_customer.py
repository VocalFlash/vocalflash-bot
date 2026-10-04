import os

import requests

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


def _start_multivocale_coordinator_on_boot():
    """Resume due Redis batches after a process restart when config is ready."""
    try:
        get_config = getattr(legacy, "get_config", None)
        if not callable(get_config):
            return
        config = get_config()
        if isinstance(config, dict) and config.get("redis_url"):
            _multivocale_runtime.ensure_coordinator(config)
    except Exception as exc:
        legacy.log(
            "Avvio coordinator MultiVocale V2 rimandato: "
            f"{type(exc).__name__}"
        )


def _post_health(url, assistant_key, expect_private_bucket=False):
    if not url or not assistant_key:
        return False
    response = requests.post(
        url,
        headers={
            "X-VocalFlash-Assistant-Key": assistant_key,
            "Content-Type": "application/json",
        },
        json={"action": "health"},
        timeout=10,
    )
    if response.status_code != 200:
        return False
    payload = response.json()
    if payload.get("ok") is not True:
        return False
    if expect_private_bucket and payload.get("bucket_private") is not True:
        return False
    return True


def _startup_readiness_check():
    """Verify staging dependencies without logging secrets or customer data."""
    redis_ok = False
    assistant_ok = False
    storage_ok = False
    try:
        get_config = getattr(legacy, "get_config", None)
        config = get_config() if callable(get_config) else {}
        if isinstance(config, dict) and config.get("redis_url"):
            client = legacy.get_redis_client(config.get("redis_url"))
            redis_ok = bool(client and client.ping())

        assistant_key = clean_text(
            os.getenv("VOCALFLASH_ASSISTANT_API_KEY", "")
        )
        assistant_ok = _post_health(
            clean_text(os.getenv("VOCALFLASH_ASSISTANT_API_URL", "")),
            assistant_key,
        )
        storage_ok = _post_health(
            clean_text(
                os.getenv("VOCALFLASH_MULTIVOCALE_STORAGE_API_URL", "")
            ),
            assistant_key,
            expect_private_bucket=True,
        )
    except Exception as exc:
        legacy.log(
            "Readiness staging exception: " + type(exc).__name__
        )

    legacy.log(
        "Readiness staging: "
        f"redis={'ok' if redis_ok else 'ko'} "
        f"assistant={'ok' if assistant_ok else 'ko'} "
        f"storage={'ok' if storage_ok else 'ko'}"
    )
    return redis_ok and assistant_ok and storage_ok


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
_start_multivocale_coordinator_on_boot()
_startup_readiness_check()
