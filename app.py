import os
import time
import threading

import requests
from flask import Flask, request, redirect


app = Flask(__name__)

# ==================================================
# CONFIGURAZIONE
# ==================================================

META_GRAPH_VERSION = "v26.0"

VOCALFLASH_API_URL = (
    "https://api.vocalflash.it/api/v1/transcribe"
)

PRIVACY_URL = "https://www.vocalflash.it/privacy.html"

META_TIMEOUT = (10, 60)
VOCALFLASH_API_TIMEOUT = (15, 240)

MAX_FILES = 5
MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024

SESSION_TTL = 20 * 60
SEEN_TTL = 60 * 60

# Raccolte MultiVocale.
# ATTENZIONE: memoria temporanea, non condivisa tra processi.
sessions = {}

# Ultimo vocale singolo per ogni utente:
# permette di includerlo quando viene premuto
# "Riepiloga più vocali".
last_audio = {}

# Protezione dai webhook duplicati di Meta.
seen_messages = {}

state_lock = threading.RLock()


# ==================================================
# LOG E CONFIGURAZIONE
# ==================================================

def log(message):
    """Evita di registrare contenuti vocali e credenziali."""
    print(message, flush=True)


def get_config():
    return {
        "wa_token": os.getenv("WA_TOKEN", "").strip(),
        "phone_id": os.getenv("WA_PHONE_ID", "").strip(),
        "api_key": os.getenv(
            "VOCALFLASH_INTERNAL_API_KEY", ""
        ).strip(),
    }


# ==================================================
# SCADENZE E DUPLICATI
# ==================================================

def cleanup_expired():
    now = time.monotonic()

    with state_lock:
        for sender, state in list(sessions.items()):
            if now - state["updated"] > SESSION_TTL:
                del sessions[sender]

        for sender, item in list(last_audio.items()):
            if now - item["updated"] > SESSION_TTL:
                del last_audio[sender]

        for message_id, timestamp in list(
            seen_messages.items()
        ):
            if now - timestamp > SEEN_TTL:
                del seen_messages[message_id]


def is_duplicate(message_id):
    now = time.monotonic()

    with state_lock:
        if message_id in seen_messages:
            return True

        seen_messages[message_id] = now

    return False


# ==================================================
# INVIO MESSAGGI WHATSAPP
# ==================================================

def send_whatsapp_message(
    phone_id,
    token,
    recipient,
    text,
    buttons=None,
):
    url = (
        "https://graph.facebook.com/"
        f"{META_GRAPH_VERSION}/{phone_id}/messages"
    )

    payload = {
        "messaging_product": "whatsapp",
        "to": recipient,
    }

    if buttons:
        payload.update({
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {
                    "text": text[:1024],
                },
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": button_id,
                                "title": title,
                            },
                        }
                        for button_id, title in buttons
                    ],
                },
            },
        })

    else:
        payload.update({
            "type": "text",
            "text": {
                "body": text[:4000],
            },
        })

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=META_TIMEOUT,
    )

    log(
        f"Invio WhatsApp: HTTP {response.status_code}"
    )

    response.raise_for_status()


def send_to_user(config, recipient, text, buttons=None):
    send_whatsapp_message(
        config["phone_id"],
        config["wa_token"],
        recipient,
        text,
        buttons,
    )


# ==================================================
# DOWNLOAD AUDIO DA META
# ==================================================

def download_whatsapp_audio(audio_id, token):
    headers = {
        "Authorization": f"Bearer {token}",
    }

    media_info_url = (
        "https://graph.facebook.com/"
        f"{META_GRAPH_VERSION}/{audio_id}"
    )

    media_info_response = requests.get(
        media_info_url,
        headers=headers,
        timeout=META_TIMEOUT,
    )

    media_info_response.raise_for_status()

    media_info = media_info_response.json()
    media_url = media_info.get("url")

    if not media_url:
        raise ValueError("URL del vocale mancante")

    audio_response = requests.get(
        media_url,
        headers=headers,
        timeout=META_TIMEOUT,
    )

    audio_response.raise_for_status()

    audio_bytes = audio_response.content

    if not audio_bytes:
        raise ValueError("Il file audio è vuoto")

    if len(audio_bytes) > MAX_FILE_BYTES:
        raise ValueError(
            "Il file audio supera il limite consentito"
        )

    mime_type = str(
        media_info.get("mime_type") or "audio/ogg"
    ).lower()

    if "mpeg" in mime_type or "mp3" in mime_type:
        filename = "audio.mp3"
        upload_mime = "audio/mpeg"

    elif "mp4" in mime_type or "m4a" in mime_type:
        filename = "audio.m4a"
        upload_mime = "audio/mp4"

    elif "wav" in mime_type:
        filename = "audio.wav"
        upload_mime = "audio/wav"

    elif "webm" in mime_type:
        filename = "audio.webm"
        upload_mime = "audio/webm"

    elif "flac" in mime_type:
        filename = "audio.flac"
        upload_mime = "audio/flac"

    else:
        filename = "audio.ogg"
        upload_mime = "audio/ogg"

    log(
        f"Audio ricevuto: {len(audio_bytes)} byte"
    )

    return filename, audio_bytes, upload_mime


# ==================================================
# FORMATTAZIONE RISPOSTA
# ==================================================

def format_items(items):
    if not isinstance(items, list):
        return ""

    lines = []

    for item in items:
        if isinstance(item, str):
            value = item.strip()

        elif isinstance(item, dict):
            value = str(
                item.get("text")
                or item.get("value")
                or item.get("title")
                or item.get("description")
                or ""
            ).strip()

            status = str(
                item.get("status") or ""
            ).lower()

            if status in (
                "proposto",
                "proposta",
                "proposed",
            ):
                value += " (proposto)"

            elif status in (
                "incerto",
                "incerta",
                "uncertain",
            ):
                value += " (da confermare)"

            detail_type = str(
                item.get("type") or ""
            ).lower()

            if detail_type in (
                "appuntamento",
                "scadenza",
                "evento",
            ):
                value = (
                    f"{detail_type.capitalize()}: {value}"
                )

        else:
            continue

        if value:
            lines.append(f"• {value}")

    return "\n".join(lines)


def build_whatsapp_response(api_data):
    if (
        not isinstance(api_data, dict)
        or api_data.get("ok") is not True
    ):
        raise ValueError("Risposta API non valida")

    summary = str(
        api_data.get("summary") or ""
    ).strip()

    if not summary:
        raise ValueError("Sintesi mancante")

    sections = [
        "⚡ *VocalFlash*",
        f"📌 *IN SINTESI*\n{summary}",
    ]

    salient_points = format_items(
        api_data.get("salient_points")
    )

    important_details = format_items(
        api_data.get("important_details")
    )

    if salient_points:
        sections.append(
            "🔑 *PUNTI SALIENTI*\n"
            f"{salient_points}"
        )

    if important_details:
        sections.append(
            "🗓️ *DETTAGLI IMPORTANTI*\n"
            f"{important_details}"
        )

    return "\n\n".join(sections)


# ==================================================
# API CENTRALE VOCALFLASH
# ==================================================

def process_audio_with_vocalflash(
    audio_files,
    api_key,
):
    """
    Invia uno o più file audio alla stessa richiesta API.

    Ogni elemento:
    (filename, audio_bytes, mime_type)
    """

    if not audio_files:
        raise ValueError("Nessun audio da elaborare")

    multipart_files = [
        (
            "file",
            (
                filename,
                audio_bytes,
                mime_type,
            ),
        )
        for filename, audio_bytes, mime_type
        in audio_files
    ]

    log(
        "Invio API VocalFlash: "
        f"{len(audio_files)} file audio"
    )

    response = requests.post(
        VOCALFLASH_API_URL,
        headers={
            "X-API-Key": api_key,
        },
        files=multipart_files,
        timeout=VOCALFLASH_API_TIMEOUT,
    )

    log(
        f"API VocalFlash: HTTP {response.status_code}"
    )

    response.raise_for_status()

    api_data = response.json()

    credits_used = api_data.get("credits_used")

    if credits_used is not None:
        log(
            "API VocalFlash: "
            f"credits_used={credits_used}"
        )

    return build_whatsapp_response(api_data)


# ==================================================
# PULSANTI
# ==================================================

MULTI_START_BUTTONS = [
    (
        "vf_multi",
        "Riepiloga più vocali",
    ),
]

MULTI_BUTTONS = [
    (
        "vf_more",
        "Aggiungi vocali",
    ),
    (
        "vf_finish",
        "Riepiloga ora",
    ),
    (
        "vf_cancel",
        "Annulla",
    ),
]

MULTI_FINISH_BUTTONS = [
    (
        "vf_finish",
        "Riepiloga ora",
    ),
    (
        "vf_cancel",
        "Annulla",
    ),
]


# ==================================================
# UTILITÀ RACCOLTA
# ==================================================

def new_session(files=None):
    return {
        "files": list(files or []),
        "updated": time.monotonic(),
        "processing": False,
    }


def collection_message(count):
    return (
        f"🎙️ *Vocali raccolti: {count}/{MAX_FILES}*\n\n"
        "Puoi inoltrare altri vocali oppure "
        "premere *Riepiloga ora* per ottenere "
        "un'unica sintesi."
    )


# ==================================================
# GESTIONE MESSAGGI
# ==================================================

def handle_message(message, config):
    sender = message.get("from")
    message_id = message.get("id")

    if not sender or not message_id:
        return

    cleanup_expired()

    if is_duplicate(message_id):
        log("Messaggio duplicato ignorato")
        return

    message_type = message.get("type")

    log(
        f"Messaggio ricevuto: tipo={message_type}"
    )

    action = ""

    # Risposte ai pulsanti.
    if message_type == "interactive":
        interactive = message.get("interactive") or {}

        reply = (
            interactive.get("button_reply")
            or interactive.get("list_reply")
            or {}
        )

        action = str(
            reply.get("id") or ""
        ).strip().lower()

    # Comandi testuali alternativi.
    elif message_type == "text":
        action = str(
            (message.get("text") or {}).get("body")
            or ""
        ).strip().lower()

    # ==================================================
    # ATTIVAZIONE MULTIVOCALE
    # ==================================================

    if action in (
        "vf_multi",
        "multi",
        "riepiloga più vocali",
    ):
        with state_lock:
            existing = sessions.get(sender)

            if existing and existing["processing"]:
                busy = True
                count = 0

            elif existing:
                busy = False
                existing["updated"] = time.monotonic()
                count = len(existing["files"])

            else:
                busy = False

                previous = last_audio.get(sender)

                initial_files = []

                if previous:
                    initial_files = [
                        previous["file"]
                    ]

                sessions[sender] = new_session(
                    initial_files
                )

                count = len(initial_files)

        if busy:
            send_to_user(
                config,
                sender,
                "Sto già preparando il riepilogo. "
                "Attendi il risultato.",
            )
            return

        if count:
            text = (
                "🎙️ *MultiVocale attivato*\n\n"
                "Ho conservato anche il vocale "
                "che ti ho appena sintetizzato.\n\n"
                f"*Vocali raccolti: {count}/{MAX_FILES}*\n\n"
                "Inoltrami gli altri vocali e, "
                "quando hai finito, premi "
                "*Riepiloga ora*.\n\n"
                "La raccolta scade dopo 20 minuti."
            )

        else:
            text = (
                "🎙️ *MultiVocale attivato*\n\n"
                "Inoltrami fino a 5 vocali.\n"
                "Quando hai finito, premi "
                "*Riepiloga ora*.\n\n"
                "La raccolta scade dopo 20 minuti."
            )

        send_to_user(
            config,
            sender,
            text,
            MULTI_BUTTONS,
        )

        return

    # ==================================================
    # ANNULLAMENTO
    # ==================================================

    if action in (
        "vf_cancel",
        "annulla",
    ):
        with state_lock:
            sessions.pop(sender, None)
            last_audio.pop(sender, None)

        send_to_user(
            config,
            sender,
            "Raccolta annullata. "
            "I vocali temporanei sono stati rimossi.",
        )

        return

    # ==================================================
    # AGGIUNGI VOCALI
    # ==================================================

    if action == "vf_more":
        with state_lock:
            state = sessions.get(sender)

            if state:
                state["updated"] = time.monotonic()
                count = len(state["files"])

            else:
                count = 0

        if state:
            text = (
                f"🎙️ Vocali raccolti: {count}/{MAX_FILES}.\n\n"
                "Inoltrami gli altri vocali. "
                "Quando hai finito, premi "
                "*Riepiloga ora*."
            )

            buttons = MULTI_BUTTONS

        else:
            text = (
                "La raccolta non è più disponibile.\n"
                "Invia un nuovo vocale oppure "
                "scrivi MULTI per ricominciare."
            )

            buttons = None

        send_to_user(
            config,
            sender,
            text,
            buttons,
        )

        return

    # ==================================================
    # RIEPILOGA ORA
    # ==================================================

    if action in (
        "vf_finish",
        "riepiloga",
    ):
        with state_lock:
            state = sessions.get(sender)

            if not state:
                audio_files = []

            elif state["processing"]:
                log(
                    "Riepilogo già in elaborazione"
                )
                return

            else:
                audio_files = list(
                    state["files"]
                )

                if audio_files:
                    state["processing"] = True

        if not audio_files:
            send_to_user(
                config,
                sender,
                "Non ci sono vocali da riepilogare.\n"
                "Inoltra prima almeno un vocale.",
            )
            return

        try:
            log(
                "Avvio sintesi multipla: "
                f"{len(audio_files)} vocali"
            )

            answer = process_audio_with_vocalflash(
                audio_files,
                config["api_key"],
            )

            send_to_user(
                config,
                sender,
                answer,
            )

            with state_lock:
                if sessions.get(sender) is state:
                    sessions.pop(sender, None)

                last_audio.pop(sender, None)

            log(
                "Sintesi multipla completata: "
                f"{len(audio_files)} vocali"
            )

        except Exception as exc:
            log(
                "Sintesi multipla non riuscita: "
                f"{type(exc).__name__}"
            )

            with state_lock:
                if sessions.get(sender) is state:
                    state["processing"] = False

            send_to_user(
                config,
                sender,
                "Non sono riuscito a generare "
                "la sintesi unica.\n"
                "Puoi riprovare premendo "
                "*Riepiloga ora* oppure annullare.",
                MULTI_FINISH_BUTTONS,
            )

        return

    # ==================================================
    # IGNORA MESSAGGI NON AUDIO
    # ==================================================

    if message_type != "audio":
        return

    audio_id = (
        message.get("audio") or {}
    ).get("id")

    if not audio_id:
        log(
            "Messaggio audio senza media ID"
        )
        return

    # ==================================================
    # CONTROLLO RACCOLTA PRIMA DEL DOWNLOAD
    # ==================================================

    with state_lock:
        state = sessions.get(sender)

        processing = bool(
            state and state["processing"]
        )

        full = bool(
            state
            and len(state["files"]) >= MAX_FILES
        )

    if processing:
        send_to_user(
            config,
            sender,
            "Sto elaborando la raccolta.\n"
            "Attendi il riepilogo prima "
            "di inviare altri vocali.",
        )
        return

    if full:
        send_to_user(
            config,
            sender,
            "Hai raggiunto il limite "
            "di 5 vocali.\n"
            "Premi *Riepiloga ora*.",
            MULTI_FINISH_BUTTONS,
        )
        return

    # ==================================================
    # DOWNLOAD AUDIO
    # ==================================================

    try:
        audio_file = download_whatsapp_audio(
            audio_id,
            config["wa_token"],
        )

    except Exception as exc:
        log(
            "Download audio non riuscito: "
            f"{type(exc).__name__}"
        )

        send_to_user(
            config,
            sender,
            "Non sono riuscito a ricevere "
            "questo vocale.\n"
            "Riprova con un file più piccolo.",
        )
        return

    # ==================================================
    # SALVATAGGIO NELLA RACCOLTA
    # ==================================================

    with state_lock:
        state = sessions.get(sender)

        if state and not state["processing"]:
            total_size = sum(
                len(item[1])
                for item in state["files"]
            )

            if len(state["files"]) >= MAX_FILES:
                result = "full"
                count = len(state["files"])

            elif (
                total_size + len(audio_file[1])
                > MAX_TOTAL_BYTES
            ):
                result = "too_large"
                count = len(state["files"])

            else:
                state["files"].append(audio_file)
                state["updated"] = time.monotonic()

                count = len(state["files"])
                result = "collected"

        elif state and state["processing"]:
            result = "processing"
            count = 0

        else:
            result = "single"
            count = 0

    if result == "processing":
        send_to_user(
            config,
            sender,
            "Sto già preparando il riepilogo. "
            "Attendi il risultato.",
        )
        return

    if result == "full":
        send_to_user(
            config,
            sender,
            "Hai raggiunto il limite "
            "di 5 vocali.\n"
            "Premi *Riepiloga ora*.",
            MULTI_FINISH_BUTTONS,
        )
        return

    if result == "too_large":
        send_to_user(
            config,
            sender,
            "La raccolta ha raggiunto "
            "il limite di dimensione.\n"
            "Premi *Riepiloga ora*.",
            MULTI_FINISH_BUTTONS,
        )
        return

    # ==================================================
    # CONFERMA RACCOLTA MULTIVOCALE
    # ==================================================

    if result == "collected":
        log(
            "MultiVocale: "
            f"{count}/{MAX_FILES} audio raccolti"
        )

        send_to_user(
            config,
            sender,
            collection_message(count),
            MULTI_BUTTONS,
        )

        return

    # ==================================================
    # VOCALE SINGOLO
    # ==================================================

    # Conserva il primo audio prima della sintesi:
    # sarà disponibile quando l'utente premerà
    # "Riepiloga più vocali".
    with state_lock:
        last_audio[sender] = {
            "file": audio_file,
            "updated": time.monotonic(),
        }

    try:
        answer = process_audio_with_vocalflash(
            [audio_file],
            config["api_key"],
        )

    except Exception as exc:
        log(
            "Sintesi singola non riuscita: "
            f"{type(exc).__name__}"
        )

        with state_lock:
            current = last_audio.get(sender)

            if (
                current
                and current["file"] is audio_file
            ):
                last_audio.pop(sender, None)

        send_to_user(
            config,
            sender,
            "⚡ *VocalFlash*\n\n"
            "Non sono riuscito a elaborare "
            "questo vocale. Riprova tra poco.",
        )
        return

    send_to_user(
        config,
        sender,
        answer,
    )

    send_to_user(
        config,
        sender,
        "Vuoi creare un riepilogo unico "
        "di più vocali?\n\n"
        "Conserverò anche questo primo vocale.",
        MULTI_START_BUTTONS,
    )


# ==================================================
# WEBHOOK META
# ==================================================

@app.route("/", methods=["GET", "POST"])
@app.route("/whatsapp", methods=["GET", "POST"])
@app.route("/webhook", methods=["GET", "POST"])
def whatsapp():
    log(
        "Richiesta ricevuta: "
        f"{request.method} {request.path}"
    )

    # HEAD e controlli di disponibilità.
    if request.method == "HEAD":
        return "ok", 200

    # Verifica webhook Meta.
    if request.method == "GET":
        if "hub.challenge" not in request.args:
            return "VocalFlash bot is running!"

        verify_token = os.getenv(
            "WA_VERIFY_TOKEN", ""
        ).strip()

        if not verify_token:
            log(
                "Configurazione webhook mancante"
            )
            return "error", 500

        if (
            request.args.get("hub.verify_token")
            == verify_token
        ):
            log("Webhook verificato")

            return request.args.get(
                "hub.challenge"
            )

        log("Verifica webhook fallita")
        return "error", 403

    # Ricezione webhook.
    data = request.get_json(
        silent=True
    )

    if not isinstance(data, dict):
        log("Webhook con JSON non valido")
        return "ok", 200

    # Non registriamo il JSON completo:
    # potrebbe contenere dati personali.
    config = get_config()

    if not all(config.values()):
        log("Configurazione incompleta")
        return "error", 500

    try:
        for entry in data.get("entry") or []:
            for change in (
                entry.get("changes") or []
            ):
                value = (
                    change.get("value") or {}
                )

                for message in (
                    value.get("messages") or []
                ):
                    handle_message(
                        message,
                        config,
                    )

    except Exception as exc:
        log(
            "Errore durante la gestione "
            "del webhook: "
            f"{type(exc).__name__}"
        )
        return "error", 500

    return "ok", 200


# ==================================================
# PRIVACY
# ==================================================

@app.route(
    "/privacy",
    methods=["GET"],
)
def privacy():
    return redirect(
        PRIVACY_URL,
        code=302,
    )


# ==================================================
# AVVIO
# ==================================================

if __name__ == "__main__":
    port = int(
        os.getenv("PORT", "10000")
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )
