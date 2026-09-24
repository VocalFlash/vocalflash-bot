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

# Raccolta automatica dei vocali inoltrati insieme.
# Ogni nuovo arrivo fa ripartire l'attesa.
AUTO_BATCH_SECONDS = 5.0

# Attesa massima per download già avviati.
FINISH_WAIT_SECONDS = 90.0

# ATTENZIONE:
# Lo stato è temporaneo e conservato in memoria.
# Su Render utilizzare un solo worker Gunicorn.
# Non sopravvive ai riavvii e non è condiviso tra processi.
sessions = {}
auto_batches = {}
queued_batches = {}
last_audio = {}
seen_messages = {}

state_lock = threading.RLock()
state_condition = threading.Condition(state_lock)


# ==================================================
# LOG
# ==================================================

def log(message):
    """
    Non registrare contenuti vocali,
    numeri di telefono o credenziali.
    """
    print(message, flush=True)


# ==================================================
# CONFIGURAZIONE AMBIENTE
# ==================================================

def get_config():
    return {
        "wa_token": os.getenv(
            "WA_TOKEN", ""
        ).strip(),

        "phone_id": os.getenv(
            "WA_PHONE_ID", ""
        ).strip(),

        "api_key": os.getenv(
            "VOCALFLASH_INTERNAL_API_KEY", ""
        ).strip(),
    }


# ==================================================
# STATO DELLE RACCOLTE
# ==================================================

def new_session(files=None):
    now = time.monotonic()

    return {
        "files": list(files or []),
        "updated": now,
        "last_arrival": now,
        "pending": 0,
        "processing": False,
        "finishing": False,
        "next_order": 0,
        "file_order": {},
        "ready_answer": None,
        "retry_needed": False,
    }


def new_auto_batch():
    now = time.monotonic()

    return {
        "files": [],
        "updated": now,
        "last_arrival": now,
        "pending": 0,
        "processing": False,
        "worker_started": False,
        "failed": 0,
        "next_order": 0,
        "file_order": {},
        "ready_answer": None,
        "retry_needed": False,
    }


def reserve_order(state):
    order = state["next_order"]
    state["next_order"] += 1
    return order


def add_ordered_file(state, audio_file, order):
    state["file_order"][id(audio_file)] = order
    state["files"].append(audio_file)
    state["files"].sort(
        key=lambda item: state["file_order"].get(id(item), -1)
    )


def promote_queued_batch(sender, config):
    """Avvia la raccolta successiva solo dopo la precedente."""
    with state_condition:
        queued = queued_batches.get(sender)
        if not queued or sender in sessions or sender in auto_batches:
            return
        queued_batches.pop(sender, None)
        auto_batches[sender] = queued
        state_condition.notify_all()
    start_auto_worker(sender, queued, config)


def cleanup_expired():
    now = time.monotonic()

    with state_condition:

        for sender, state in list(sessions.items()):
            if (
                state["pending"] > 0
                or state["processing"]
                or state["finishing"]
            ):
                continue

            if now - state["updated"] > SESSION_TTL:
                sessions.pop(sender, None)

        for sender, state in list(auto_batches.items()):
            if (
                state["pending"] > 0
                or state["processing"]
                or state["worker_started"]
            ):
                continue

            if now - state["updated"] > SESSION_TTL:
                auto_batches.pop(sender, None)

        for sender, state in list(queued_batches.items()):
            if state["pending"] == 0 and now - state["updated"] > SESSION_TTL:
                queued_batches.pop(sender, None)

        for sender, item in list(last_audio.items()):
            if now - item["updated"] > SESSION_TTL:
                last_audio.pop(sender, None)

        for message_id, timestamp in list(
            seen_messages.items()
        ):
            if now - timestamp > SEEN_TTL:
                seen_messages.pop(message_id, None)


def is_duplicate(message_id):
    now = time.monotonic()

    with state_condition:
        if message_id in seen_messages:
            return True

        seen_messages[message_id] = now

    return False


def collection_message(count):
    return (
        f"🎙️ *Vocali raccolti: {count}/{MAX_FILES}*\n\n"
        "Puoi inoltrare altri vocali oppure "
        "premere *Riepiloga ora* per ottenere "
        "un'unica sintesi."
    )


# ==================================================
# INVIO WHATSAPP
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


def send_to_user(
    config,
    recipient,
    text,
    buttons=None,
):
    send_whatsapp_message(
        config["phone_id"],
        config["wa_token"],
        recipient,
        text,
        buttons,
    )


def safe_send(
    config,
    recipient,
    text,
    buttons=None,
):
    try:
        send_to_user(
            config,
            recipient,
            text,
            buttons,
        )
        return True

    except Exception as exc:
        log(
            "Invio WhatsApp non riuscito: "
            f"{type(exc).__name__}"
        )
        return False


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
            "Il file audio supera il limite"
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
# FORMATTAZIONE
# ==================================================

def clean_text(value):
    return str(value or "").strip()


def normalize_for_comparison(value):
    """
    Normalizzazione semplice per evitare
    ripetizioni identiche.
    """
    return " ".join(
        clean_text(value).lower().split()
    ).strip(" .,:;!?")


def extract_item_value(item):
    if isinstance(item, str):
        return item.strip()

    if isinstance(item, dict):
        return clean_text(
            item.get("text")
            or item.get("value")
            or item.get("title")
            or item.get("description")
        )

    return ""


def format_items(items):
    if not isinstance(items, list):
        return ""

    lines = []
    seen = set()

    for item in items:

        value = extract_item_value(item)

        if not value:
            continue

        if isinstance(item, dict):
            status = clean_text(
                item.get("status")
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

            detail_type = clean_text(
                item.get("type")
            ).lower()

            if detail_type in (
                "appuntamento",
                "scadenza",
                "evento",
            ):
                value = (
                    f"{detail_type.capitalize()}: {value}"
                )

        key = normalize_for_comparison(value)

        if not key or key in seen:
            continue

        seen.add(key)
        lines.append(f"• {value}")

    return "\n".join(lines)


def comparison_tokens(value):
    """Confronto conservativo per evitare ripetizioni evidenti."""
    import re
    text = clean_text(value).lower()
    text = re.sub(r"(?<=\d)[.:](?=\d{2}\b)", "", text)
    return re.findall(r"[a-zà-ÿ0-9]+", text)


def already_expressed(value, previous):
    """Non eliminare informazioni diverse solo perché condividono parole."""
    tokens = comparison_tokens(value)
    if not tokens:
        return True
    for existing in previous:
        existing_tokens = comparison_tokens(existing)
        if not existing_tokens:
            continue
        if tokens == existing_tokens:
            return True
        # Dettagli brevi (luogo/orario) già esplicitati nella sintesi.
        if len(tokens) <= 3 and any(
            existing_tokens[i:i + len(tokens)] == tokens
            for i in range(len(existing_tokens) - len(tokens) + 1)
        ):
            return True
        # Evita di ripetere frasi sostanzialmente uguali; per i testi
        # lunghi richiediamo una corrispondenza molto elevata.
        if len(tokens) >= 5:
            common = len(set(tokens) & set(existing_tokens))
            if common / len(set(tokens)) >= 0.9:
                return True
    return False


def format_task(item, contextual_texts=None):
    """Conserva le attività nel formato WhatsApp a due sezioni.

    Deadline e orari già presenti nel contesto generale non vengono
    riattribuiti al task: evita che un riferimento temporale di un
    appuntamento venga ereditato da un promemoria separato.
    """
    if isinstance(item, str):
        return clean_text(item)
    if not isinstance(item, dict):
        return ""

    title = clean_text(
        item.get("title") or item.get("text") or item.get("value")
    )
    if not title:
        return ""

    contextual_texts = list(contextual_texts or [])
    deadline = clean_text(item.get("deadline"))
    task_time = clean_text(item.get("time"))
    status = clean_text(item.get("status")).lower()
    additions = []

    # Se una deadline/orario compare già nel riepilogo o nei dettagli
    # generali, non la riattribuiamo automaticamente al task. Se invece
    # è esplicitamente contenuta nel titolo del task, resta visibile lì.
    if (
        deadline
        and not already_expressed(deadline, [title])
        and not already_expressed(deadline, contextual_texts)
    ):
        additions.append(deadline)

    if (
        task_time
        and not already_expressed(task_time, [title] + additions)
        and not already_expressed(task_time, contextual_texts)
    ):
        additions.append(task_time)

    if additions:
        title += " — " + ", ".join(additions)

    if status in ("proposto", "proposta", "proposed"):
        title += " (proposto)"
    elif status in ("incerto", "incerta", "uncertain"):
        title += " (da confermare)"

    return title


def build_whatsapp_response(api_data):
    if not isinstance(api_data, dict) or api_data.get("ok") is not True:
        raise ValueError("Risposta API non valida")

    summary = clean_text(api_data.get("summary"))
    if not summary:
        raise ValueError("Sintesi mancante")

    # Leggiamo prima i task per evitare che la stessa attività venga
    # mostrata anche come salient_point nella sezione IN SINTESI.
    tasks = api_data.get("tasks")
    task_titles = []
    if isinstance(tasks, list):
        for item in tasks:
            if isinstance(item, dict):
                title = clean_text(
                    item.get("title") or item.get("text") or item.get("value")
                )
            else:
                title = clean_text(item)
            if title:
                task_titles.append(title)

    # Il riassunto resta il testo principale. Aggiungiamo solo i punti
    # realmente nuovi e che non rappresentano già un task.
    summary_parts = [summary]
    summary_seen = [summary]
    salient_points = api_data.get("salient_points")
    if isinstance(salient_points, list):
        for item in salient_points:
            point = extract_item_value(item)
            if not point:
                continue
            if already_expressed(point, summary_seen):
                continue
            if already_expressed(point, task_titles) or already_expressed(
                point, [f"Ricordarsi di {title}" for title in task_titles]
            ):
                continue
            summary_parts.append(f"• {point}")
            summary_seen.append(point)

    # I dettagli e i promemoria condividono la stessa sezione visibile.
    detail_lines = []
    detail_seen = list(summary_seen)
    important_details = api_data.get("important_details")
    if isinstance(important_details, list):
        for item in important_details:
            value = extract_item_value(item)
            if not value:
                continue
            if isinstance(item, dict):
                status = clean_text(item.get("status")).lower()
                if status in ("proposto", "proposta", "proposed"):
                    value += " (proposto)"
                elif status in ("incerto", "incerta", "uncertain"):
                    value += " (da confermare)"
                detail_type = clean_text(item.get("type")).lower()
                if detail_type in ("appuntamento", "scadenza", "evento"):
                    value = f"{detail_type.capitalize()}: {value}"
            if not already_expressed(value, detail_seen):
                detail_lines.append(f"• {value}")
                detail_seen.append(value)

    # Per i riferimenti temporali dei task usiamo come contesto il
    # riepilogo e i dettagli generali: se il riferimento è già lì,
    # non lo attribuiamo una seconda volta al task.
    task_context = list(detail_seen)

    if isinstance(tasks, list):
        for item in tasks:
            task = format_task(item, task_context)
            if task and not already_expressed(task, detail_seen):
                detail_lines.append(f"• {task}")
                detail_seen.append(task)

    sections = [
        "⚡ *VocalFlash*",
        "📌 *IN SINTESI*\n" + "\n".join(summary_parts),
    ]

    if detail_lines:
        sections.append(
            "🗓️ *DETTAGLI IMPORTANTI*\n" + "\n".join(detail_lines)
        )

    return "\n\n".join(sections)


# ==================================================
# INVIO ALL'API CENTRALE
# ==================================================

def process_audio_with_vocalflash(
    audio_files,
    api_key,
):
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

    credits_used = api_data.get(
        "credits_used"
    )

    if credits_used is not None:
        log(
            "API VocalFlash: "
            f"credits_used={credits_used}"
        )

    return build_whatsapp_response(
        api_data
    )


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
# MULTIVOCALE MANUALE
# ==================================================

def activate_multi(sender, config):
    with state_condition:

        existing = sessions.get(sender)
        automatic = auto_batches.get(sender)

        if existing and (
            existing["processing"]
            or existing["finishing"]
        ):
            busy = True
            count = 0

        elif automatic and (
            automatic["pending"] > 0
            or automatic["processing"]
        ):
            busy = True
            count = 0

        elif existing:
            busy = False

            existing["updated"] = (
                time.monotonic()
            )

            count = len(
                existing["files"]
            )

        else:
            busy = False

            previous = last_audio.get(sender)

            initial_files = []

            if previous:
                initial_files = [
                    previous["file"]
                ]

            sessions[sender] = new_session(initial_files)
            if initial_files:
                sessions[sender]["file_order"][id(initial_files[0])] = -1

            count = len(
                initial_files
            )

            log(
                "MultiVocale attivato: "
                f"{count} audio iniziali"
            )

    if busy:
        safe_send(
            config,
            sender,
            "Sto già ricevendo o elaborando "
            "dei vocali. Attendi il riepilogo.",
        )
        return

    if count:
        text = (
            "🎙️ *MultiVocale attivato*\n\n"
            "Ho conservato anche il primo vocale "
            "che ti ho appena sintetizzato.\n\n"
            f"*Vocali raccolti: {count}/{MAX_FILES}*\n\n"
            "Inoltrami gli altri vocali e premi "
            "*Riepiloga ora* quando hai finito."
        )

    else:
        text = (
            "🎙️ *MultiVocale attivato*\n\n"
            "Inoltrami fino a 5 vocali, "
            "anche più di uno contemporaneamente.\n\n"
            "Quando hai finito, premi "
            "*Riepiloga ora*."
        )

    safe_send(
        config,
        sender,
        text,
        MULTI_BUTTONS,
    )


# ==================================================
# ANNULLAMENTO
# ==================================================

def cancel_multi(sender, config):
    with state_condition:

        state = sessions.get(sender)

        if state and state["processing"]:
            cannot_cancel = True

        else:
            cannot_cancel = False

            sessions.pop(sender, None)
            last_audio.pop(sender, None)

            state_condition.notify_all()

    if cannot_cancel:
        safe_send(
            config,
            sender,
            "La sintesi è già in elaborazione. "
            "Attendi il risultato.",
        )
        return

    safe_send(
        config,
        sender,
        "Raccolta annullata. "
        "I vocali temporanei sono stati rimossi.",
    )


# ==================================================
# AGGIUNGI VOCALI
# ==================================================

def more_multi(sender, config):
    with state_condition:

        state = sessions.get(sender)

        if state:
            state["updated"] = (
                time.monotonic()
            )

            count = len(
                state["files"]
            )

            finishing = state["finishing"]
            processing = state["processing"]

        else:
            count = 0
            finishing = False
            processing = False

    if processing or finishing:
        safe_send(
            config,
            sender,
            "Sto preparando il riepilogo. "
            "Attendi il risultato.",
        )
        return

    if state:
        safe_send(
            config,
            sender,
            collection_message(count),
            MULTI_BUTTONS,
        )

    else:
        safe_send(
            config,
            sender,
            "La raccolta non è più disponibile.\n"
            "Invia un nuovo vocale oppure "
            "scrivi MULTI per ricominciare.",
        )


# ==================================================
# RIEPILOGO MULTIVOCALE MANUALE
# ==================================================

def finish_multi(sender, config):
    with state_condition:

        state = sessions.get(sender)

        if not state:
            missing = True
            busy = False

        elif (
            state["processing"]
            or state["finishing"]
        ):
            missing = False
            busy = True

        else:
            missing = False
            busy = False

            state["finishing"] = True
            state["updated"] = (
                time.monotonic()
            )

            state_condition.notify_all()

    if missing:
        safe_send(
            config,
            sender,
            "Non ci sono vocali da riepilogare.\n"
            "Inoltra prima almeno un vocale.",
        )
        return

    if busy:
        log("Riepilogo già richiesto")
        return

    deadline = (
        time.monotonic()
        + FINISH_WAIT_SECONDS
    )

    timed_out = False
    audio_files = []

    with state_condition:

        while True:

            if sessions.get(sender) is not state:
                log(
                    "Raccolta non più disponibile "
                    "durante l'attesa"
                )
                return

            now = time.monotonic()

            pending = state["pending"]

            quiet_for = (
                now - state["last_arrival"]
            )

            if (
                pending == 0
                and quiet_for >= AUTO_BATCH_SECONDS
            ):
                break

            remaining = deadline - now

            if remaining <= 0:
                timed_out = True
                break

            if pending > 0:
                wait_for = min(
                    remaining,
                    1.0,
                )

            else:
                wait_for = min(
                    remaining,
                    max(
                        0.1,
                        AUTO_BATCH_SECONDS - quiet_for,
                    ),
                )

            state_condition.wait(
                timeout=wait_for
            )

        if timed_out:
            state["finishing"] = False
            state["updated"] = (
                time.monotonic()
            )

        else:
            audio_files = list(
                state["files"]
            )

            if audio_files:
                state["processing"] = True

            else:
                state["finishing"] = False

        state_condition.notify_all()

    if timed_out:
        safe_send(
            config,
            sender,
            "Sto ancora ricevendo uno o più vocali.\n"
            "Attendi qualche secondo e premi "
            "nuovamente *Riepiloga ora*.",
            MULTI_FINISH_BUTTONS,
        )
        return

    if not audio_files:
        safe_send(
            config,
            sender,
            "La raccolta è vuota.\n"
            "Inoltra almeno un vocale.",
            MULTI_BUTTONS,
        )
        return

    try:
        log(
            "Avvio sintesi multipla: "
            f"{len(audio_files)} vocali"
        )

        answer = state["ready_answer"]
        if answer is None:
            answer = process_audio_with_vocalflash(
                audio_files,
                config["api_key"],
            )
            with state_condition:
                state["ready_answer"] = answer

        sent = safe_send(
            config,
            sender,
            answer,
        )

        if not sent:
            raise RuntimeError(
                "Invio sintesi WhatsApp non riuscito"
            )

        with state_condition:

            if sessions.get(sender) is state:
                sessions.pop(sender, None)

            last_audio.pop(sender, None)

            state_condition.notify_all()

        promote_queued_batch(sender, config)

        log(
            "Sintesi multipla completata: "
            f"{len(audio_files)} vocali"
        )

    except Exception as exc:

        log(
            "Sintesi multipla non riuscita: "
            f"{type(exc).__name__}"
        )

        with state_condition:

            if sessions.get(sender) is state:
                state["processing"] = False
                state["finishing"] = False
                state["updated"] = (
                    time.monotonic()
                )

                state_condition.notify_all()

        safe_send(
            config,
            sender,
            "Non sono riuscito a generare "
            "o inviare la sintesi unica.\n"
            "Puoi riprovare premendo "
            "*Riepiloga ora* oppure annullare.",
            MULTI_FINISH_BUTTONS,
        )


# ==================================================
# RACCOLTA AUTOMATICA
# ==================================================

def auto_batch_worker(
    sender,
    state,
    config,
):
    """
    Un solo thread per raccolta.

    Attende:
    - tutti i download già registrati;
    - cinque secondi dall'ultimo arrivo.

    La sintesi parte una sola volta per raccolta.
    """

    deadline = (
        time.monotonic()
        + FINISH_WAIT_SECONDS
    )

    with state_condition:

        while True:

            if auto_batches.get(sender) is not state:
                return

            now = time.monotonic()

            pending = state["pending"]

            quiet_for = (
                now - state["last_arrival"]
            )

            if (
                pending == 0
                and quiet_for >= AUTO_BATCH_SECONDS
            ):
                break

            remaining = deadline - now

            if remaining <= 0:

                # Se un download è ancora in corso,
                # non elaboriamo una raccolta incompleta.
                if pending > 0:
                    state["worker_started"] = False

                    log(
                        "Raccolta automatica: "
                        "attesa download scaduta"
                    )

                    state_condition.notify_all()
                    return

                # Nessun download pendente:
                # chiudiamo la raccolta disponibile.
                break

            if pending > 0:
                wait_for = min(
                    remaining,
                    1.0,
                )

            else:
                wait_for = min(
                    remaining,
                    max(
                        0.1,
                        AUTO_BATCH_SECONDS - quiet_for,
                    ),
                )

            state_condition.wait(
                timeout=wait_for
            )

        audio_files = list(
            state["files"]
        )

        failed_count = state["failed"]

        if not audio_files:

            if auto_batches.get(sender) is state:
                auto_batches.pop(sender, None)

            state_condition.notify_all()

            log(
                "Raccolta automatica: "
                "nessun audio disponibile"
            )

            return

        state["processing"] = True
        state["retry_needed"] = False
        state["updated"] = (
            time.monotonic()
        )

        log(
            "Raccolta automatica chiusa: "
            f"{len(audio_files)} vocali"
        )

    # Sintesi fuori dal lock.
    try:

        answer = state["ready_answer"]
        if answer is None:
            answer = process_audio_with_vocalflash(
                audio_files,
                config["api_key"],
            )
            with state_condition:
                state["ready_answer"] = answer

        sent = safe_send(
            config,
            sender,
            answer,
        )

        if not sent:
            raise RuntimeError(
                "Invio sintesi WhatsApp non riuscito"
            )

        with state_condition:

            if auto_batches.get(sender) is state:
                auto_batches.pop(sender, None)

            if len(audio_files) == 1:
                last_audio[sender] = {
                    "file": audio_files[0],
                    "updated": time.monotonic(),
                }

            else:
                last_audio.pop(sender, None)

            state_condition.notify_all()

        promote_queued_batch(sender, config)

        log(
            "Sintesi automatica completata: "
            f"{len(audio_files)} vocali"
        )

        if failed_count:
            safe_send(
                config,
                sender,
                "Attenzione: uno o più vocali "
                "non sono stati ricevuti "
                "correttamente e non sono "
                "inclusi nella sintesi.",
            )

        # Manteniamo il pulsante MultiVocale
        # dopo un singolo vocale, come prima.
        if len(audio_files) == 1:
            safe_send(
                config,
                sender,
                "Vuoi creare un riepilogo unico "
                "di più vocali?\n\n"
                "Conserverò anche questo "
                "primo vocale.",
                MULTI_START_BUTTONS,
            )

    except Exception as exc:

        log(
            "Sintesi automatica non riuscita: "
            f"{type(exc).__name__}"
        )

        with state_condition:

            if auto_batches.get(sender) is state:
                state["processing"] = False
                state["worker_started"] = False
                state["retry_needed"] = True
                state["updated"] = (
                    time.monotonic()
                )

                state_condition.notify_all()

        safe_send(
            config,
            sender,
            "Non sono riuscito a elaborare "
            "questa raccolta.\n"
            "Premi Riprova per ritentare senza reinviare i vocali.",
            [("vf_retry", "Riprova")],
        )


def start_auto_worker(
    sender,
    state,
    config,
):
    """
    Avvia un solo worker per raccolta.
    Deve essere chiamato fuori dal lock.
    """

    with state_condition:

        if auto_batches.get(sender) is not state:
            return

        if (
            state["worker_started"]
            or state["processing"]
        ):
            return

        state["worker_started"] = True

    try:

        worker = threading.Thread(
            target=auto_batch_worker,
            args=(
                sender,
                state,
                config,
            ),
            daemon=True,
        )

        worker.start()

    except Exception:

        with state_condition:

            if auto_batches.get(sender) is state:
                state["worker_started"] = False

                state_condition.notify_all()

        raise


# ==================================================
# GESTIONE AUDIO
# ==================================================

def handle_audio(message, sender, config):
    audio_id = (
        message.get("audio") or {}
    ).get("id")

    if not audio_id:
        log(
            "Messaggio audio senza media ID"
        )
        return

    # Registriamo l'arrivo prima del download.
    # È indispensabile per raggruppare webhook
    # ricevuti quasi contemporaneamente.

    with state_condition:

        manual = sessions.get(sender)

        if manual:

            if manual["processing"]:
                queued = queued_batches.setdefault(sender, new_auto_batch())
                state = queued
                if len(queued["files"]) + queued["pending"] >= MAX_FILES:
                    mode = "queued_full"
                else:
                    mode = "queued"
                    queued["pending"] += 1
                    order = reserve_order(queued)
                    queued["last_arrival"] = time.monotonic()
                    queued["updated"] = queued["last_arrival"]
                    state_condition.notify_all()

            elif (
                len(manual["files"])
                + manual["pending"]
                >= MAX_FILES
            ):
                mode = "manual_full"

            else:
                mode = "manual"

                manual["pending"] += 1
                order = reserve_order(manual)

                now = time.monotonic()

                manual["last_arrival"] = now
                manual["updated"] = now

                state_condition.notify_all()

                log(
                    "MultiVocale: download avviato; "
                    f"in corso={manual['pending']}"
                )

            if mode != "queued" and mode != "queued_full":
                state = manual

        else:

            automatic = auto_batches.get(sender)

            if (
                automatic
                and (automatic["processing"] or automatic["retry_needed"])
            ):
                queued = queued_batches.setdefault(sender, new_auto_batch())
                state = queued
                if len(queued["files"]) + queued["pending"] >= MAX_FILES:
                    mode = "queued_full"
                else:
                    mode = "queued"
                    queued["pending"] += 1
                    order = reserve_order(queued)
                    queued["last_arrival"] = time.monotonic()
                    queued["updated"] = queued["last_arrival"]
                    state_condition.notify_all()

            else:

                if automatic is None:
                    automatic = new_auto_batch()
                    auto_batches[sender] = automatic

                state = automatic

                if (
                    len(state["files"])
                    + state["pending"]
                    >= MAX_FILES
                ):
                    mode = "auto_full"

                else:

                    current_size = sum(
                        len(item[1])
                        for item in state["files"]
                    )

                    if current_size >= MAX_TOTAL_BYTES:
                        mode = "auto_full"

                    else:
                        mode = "auto"

                        state["pending"] += 1
                        order = reserve_order(state)

                        now = time.monotonic()

                        state["last_arrival"] = now
                        state["updated"] = now

                        state_condition.notify_all()

                        log(
                            "Raccolta automatica: "
                            "download avviato; "
                            f"in corso={state['pending']}"
                        )

    # ==================================================
    # CONTROLLI PRELIMINARI
    # ==================================================

    if mode in (
        "manual_processing",
        "auto_processing",
    ):
        safe_send(
            config,
            sender,
            "Sto già elaborando una raccolta.\n"
            "Attendi il riepilogo prima "
            "di inviare altri vocali.",
        )
        return

    if mode in (
        "manual_full",
        "auto_full",
        "queued_full",
    ):

        if mode == "queued_full":
            safe_send(config, sender, "La raccolta successiva contiene già 5 vocali. Attendi il riepilogo corrente.")
        elif mode == "manual_full":
            safe_send(
                config,
                sender,
                "Hai raggiunto il limite "
                "di 5 vocali.\n"
                "Premi *Riepiloga ora*.",
                MULTI_FINISH_BUTTONS,
            )

        else:
            safe_send(
                config,
                sender,
                "Puoi inviare al massimo "
                "5 vocali per raccolta.\n"
                "Attendi il riepilogo corrente.",
            )

        return

    # ==================================================
    # DOWNLOAD
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

        with state_condition:

            if mode == "manual":

                if sessions.get(sender) is state:
                    state["pending"] = max(
                        0,
                        state["pending"] - 1,
                    )

                    state["updated"] = (
                        time.monotonic()
                    )

            elif mode == "queued":
                if queued_batches.get(sender) is state:
                    state["pending"] = max(0, state["pending"] - 1)
                    state["failed"] += 1
                    state["updated"] = time.monotonic()

            elif mode == "auto":

                if auto_batches.get(sender) is state:
                    state["pending"] = max(
                        0,
                        state["pending"] - 1,
                    )

                    state["failed"] += 1

                    state["updated"] = (
                        time.monotonic()
                    )

            state_condition.notify_all()

        safe_send(
            config,
            sender,
            "Non sono riuscito a ricevere "
            "questo vocale.\n"
            "Riprova con un file più piccolo.",
        )

        if mode == "auto":
            start_auto_worker(
                sender,
                state,
                config,
            )

        return

    # ==================================================
    # SALVATAGGIO NELLA RACCOLTA MANUALE
    # ==================================================

    if mode == "queued":
        with state_condition:
            if queued_batches.get(sender) is not state:
                return
            state["pending"] = max(0, state["pending"] - 1)
            total_size = sum(len(item[1]) for item in state["files"])
            if total_size + len(audio_file[1]) > MAX_TOTAL_BYTES:
                state["failed"] += 1
                accepted = False
            else:
                add_ordered_file(state, audio_file, order)
                accepted = True
            state["updated"] = time.monotonic()
            state_condition.notify_all()
        if accepted:
            safe_send(config, sender, "Vocale conservato per il riepilogo successivo.")
        else:
            safe_send(config, sender, "La raccolta successiva ha raggiunto il limite di dimensione: questo vocale non è stato incluso.")
        promote_queued_batch(sender, config)
        return

    if mode == "manual":

        with state_condition:

            if sessions.get(sender) is not state:
                result = "cancelled"
                count = 0

            else:

                state["pending"] = max(
                    0,
                    state["pending"] - 1,
                )

                total_size = sum(
                    len(item[1])
                    for item in state["files"]
                )

                if state["processing"]:
                    result = "processing"
                    count = len(state["files"])

                elif len(state["files"]) >= MAX_FILES:
                    result = "full"
                    count = len(state["files"])

                elif (
                    total_size + len(audio_file[1])
                    > MAX_TOTAL_BYTES
                ):
                    result = "too_large"
                    count = len(state["files"])

                else:

                    add_ordered_file(state, audio_file, order)

                    count = len(state["files"])

                    state["updated"] = (
                        time.monotonic()
                    )

                    result = "collected"

                state_condition.notify_all()

        if result == "cancelled":
            log(
                "Audio ignorato: raccolta annullata"
            )
            return

        if result == "processing":
            safe_send(
                config,
                sender,
                "Sto già elaborando il riepilogo. "
                "Questo vocale non è stato incluso.",
            )
            return

        if result == "full":
            safe_send(
                config,
                sender,
                "Hai raggiunto il limite "
                "di 5 vocali.\n"
                "Premi *Riepiloga ora*.",
                MULTI_FINISH_BUTTONS,
            )
            return

        if result == "too_large":
            safe_send(
                config,
                sender,
                "La raccolta ha raggiunto "
                "il limite di dimensione.\n"
                "Premi *Riepiloga ora*.",
                MULTI_FINISH_BUTTONS,
            )
            return

        log(
            "MultiVocale: "
            f"{count}/{MAX_FILES} audio raccolti"
        )

        safe_send(
            config,
            sender,
            collection_message(count),
            MULTI_BUTTONS,
        )

        return

    # ==================================================
    # SALVATAGGIO NELLA RACCOLTA AUTOMATICA
    # ==================================================

    with state_condition:

        if auto_batches.get(sender) is not state:
            result = "cancelled"
            count = 0

        else:

            state["pending"] = max(
                0,
                state["pending"] - 1,
            )

            total_size = sum(
                len(item[1])
                for item in state["files"]
            )

            if state["processing"]:
                result = "processing"
                count = len(state["files"])

            elif len(state["files"]) >= MAX_FILES:
                result = "full"
                count = len(state["files"])

            elif (
                total_size + len(audio_file[1])
                > MAX_TOTAL_BYTES
            ):
                result = "too_large"
                count = len(state["files"])

                state["failed"] += 1

            else:

                add_ordered_file(state, audio_file, order)

                count = len(state["files"])

                state["updated"] = (
                    time.monotonic()
                )

                result = "collected"

            state_condition.notify_all()

    if result == "cancelled":
        log(
            "Audio ignorato: raccolta "
            "automatica non disponibile"
        )
        return

    if result == "processing":
        safe_send(
            config,
            sender,
            "La sintesi è già iniziata. "
            "Questo vocale non è stato incluso.",
        )
        return

    if result == "full":
        safe_send(
            config,
            sender,
            "Hai raggiunto il limite "
            "di 5 vocali.\n"
            "Attendi il riepilogo corrente.",
        )
        return

    if result == "too_large":
        safe_send(
            config,
            sender,
            "La raccolta ha raggiunto "
            "il limite di dimensione. "
            "Questo vocale non è stato incluso.",
        )

        start_auto_worker(
            sender,
            state,
            config,
        )
        return

    log(
        "Raccolta automatica: "
        f"{count}/{MAX_FILES} audio raccolti"
    )

    # Non inviamo una risposta per ogni audio:
    # l'utente riceverà un solo riepilogo finale.

    start_auto_worker(
        sender,
        state,
        config,
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
        log(
            "Messaggio duplicato ignorato"
        )
        return

    message_type = message.get("type")

    log(
        f"Messaggio ricevuto: tipo={message_type}"
    )

    action = ""

    if message_type == "interactive":

        interactive = (
            message.get("interactive") or {}
        )

        reply = (
            interactive.get("button_reply")
            or interactive.get("list_reply")
            or {}
        )

        action = clean_text(
            reply.get("id")
        ).lower()

    elif message_type == "text":

        action = clean_text(
            (message.get("text") or {}).get("body")
        ).lower()

    if action in (
        "vf_multi",
        "multi",
        "riepiloga più vocali",
    ):
        activate_multi(
            sender,
            config,
        )
        return

    if action in (
        "vf_cancel",
        "annulla",
    ):
        cancel_multi(
            sender,
            config,
        )
        return

    if action == "vf_retry":
        with state_condition:
            state = auto_batches.get(sender)
        if state and not state["processing"]:
            start_auto_worker(sender, state, config)
        else:
            safe_send(config, sender, "Nessuna raccolta da riprovare.")
        return

    if action == "vf_more":
        more_multi(
            sender,
            config,
        )
        return

    if action in (
        "vf_finish",
        "riepiloga",
    ):
        finish_multi(
            sender,
            config,
        )
        return

    if message_type == "audio":
        handle_audio(
            message,
            sender,
            config,
        )
        return

    # Gli altri tipi di messaggio
    # vengono ignorati.


# ==================================================
# WEBHOOK META
# ==================================================

@app.route(
    "/",
    methods=["GET", "POST", "HEAD"],
)
@app.route(
    "/whatsapp",
    methods=["GET", "POST", "HEAD"],
)
@app.route(
    "/webhook",
    methods=["GET", "POST", "HEAD"],
)
def whatsapp():

    log(
        "Richiesta ricevuta: "
        f"{request.method} {request.path}"
    )

    if request.method == "HEAD":
        return "ok", 200

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

    data = request.get_json(
        silent=True
    )

    if not isinstance(data, dict):
        log(
            "Webhook con JSON non valido"
        )
        return "ok", 200

    print(f"Dati: {data}", flush=True)

    config = get_config()

    if not all(config.values()):
        log(
            "Configurazione incompleta"
        )
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
        threaded=True,
    )
