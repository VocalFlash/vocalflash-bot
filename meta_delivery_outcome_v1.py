"""Conservative classification for Meta WhatsApp outbound send results.

A provider HTTP 2xx is considered accepted only when Meta returns a wamid.
Definitive 4xx rejection is distinct from ambiguous transport/server outcomes.
"""
from typing import Any


def classify_meta_response(status_code: int, payload: Any = None) -> dict:
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        return {"outcome": "outcome_unknown"}

    if 200 <= status < 300:
        messages = payload.get("messages") if isinstance(payload, dict) else None
        first = messages[0] if isinstance(messages, list) and messages else None
        message_id = first.get("id") if isinstance(first, dict) else None
        if isinstance(message_id, str) and message_id.startswith("wamid.") and len(message_id) > 6:
            return {
                "outcome": "accepted",
                "provider_message_id": message_id,
                "http_status": status,
            }
        return {"outcome": "outcome_unknown", "http_status": status}

    # A complete HTTP 4xx response means this request was rejected. 408 is
    # treated as ambiguous because a timeout can occur after provider handling.
    if 400 <= status < 500 and status != 408:
        return {"outcome": "definitive_rejection", "http_status": status}

    # 5xx, redirects not resolved by the client, and transport failures are
    # conservative unknowns: the provider may have accepted before the error.
    return {"outcome": "outcome_unknown", "http_status": status}


def classify_meta_exception(error: BaseException) -> dict:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return classify_meta_response(status)
    return {"outcome": "outcome_unknown", "error_type": type(error).__name__}
