from flask import Flask, request
import os
import traceback
import requests

app = Flask(__name__)

META_GRAPH_VERSION = "v26.0"
VOCALFLASH_API_URL = "https://api.vocalflash.it/api/v1/transcribe"

# Timeout separati per evitare che una richiesta rimanga bloccata indefinitamente.
META_TIMEOUT = (10, 60)
VOCALFLASH_API_TIMEOUT = (15, 240)


def log(message):
    print(message, flush=True)


def get_config():
    """Legge le variabili necessarie senza esporre i token nei log."""
    return {
        "wa_token": os.getenv("WA_TOKEN", "").strip(),
        "phone_id": (
            os.getenv("WA_PHONE_ID")
            or os.getenv("PHONE_NUMBER_ID")
            or "1318571384677144"
        ).strip(),
        "api_key": os.getenv("VOCALFLASH_INTERNAL_API_KEY", "").strip(),
    }


def format_list(items):
    """Converte una lista di punti in testo leggibile su WhatsApp."""
    if not isinstance(items, list):
        return ""

    lines = []

    for item in items:
        if isinstance(item, str):
            value = item.strip()

        elif isinstance(item, dict):
            # Gestione prudente di eventuali punti restituiti come oggetti.
            value = str(
                item.get("text")
                or item.get("value")
                or item.get("title")
                or item.get("description")
                or ""
            ).strip()

        else:
            continue

        if value:
            lines.append(f"• {value}")

    return "\n".join(lines)


def format_important_details(items):
    """Formatta i dettagli senza inventare date o modificare il loro significato."""
    if not isinstance(items, list):
        return ""

    lines = []

    for item in items:
        if isinstance(item, str):
            value = item.strip()
            if value:
                lines.append(f"• {value}")
            continue

        if not isinstance(item, dict):
            continue

        value = str(item.get("value") or "").strip()
        detail_type = str(item.get("type") or "").strip().lower()
        status = str(item.get("status") or "").strip().lower()

        if not value:
            continue

        # Indichiamo lo stato soltanto quando aggiunge un'informazione utile.
        status_label = ""

        if status in ("proposto", "proposta", "proposed"):
            status_label = " (proposto)"
        elif status in ("incerto", "incerta", "uncertain"):
            status_label = " (da confermare)"

        # Non convertiamo le date: "4 dicembre" deve rimanere "4 dicembre".
        # Il tipo viene mostrato solo per appuntamenti e scadenze.
        if detail_type == "appuntamento":
            line = f"• Appuntamento: {value}{status_label}"
        elif detail_type == "scadenza":
            line = f"• Scadenza: {value}{status_label}"
        else:
            line = f"• {value}{status_label}"

        lines.append(line)

    return "\n".join(lines)


def build_whatsapp_response(api_data):
    """Converte il JSON VocalFlash nel formato del prodotto WhatsApp."""
    if not isinstance(api_data, dict) or api_data.get("ok") is not True:
        raise ValueError("Risposta API VocalFlash non valida")

    summary = str(api_data.get("summary") or "").strip()

    salient_points = format_list(api_data.get("salient_points"))
    important_details = format_important_details(
        api_data.get("important_details")
    )

    if not summary:
        raise ValueError("La risposta API non contiene una sintesi")

    sections = [
        "⚡ *VocalFlash*",
        f"📌 *IN SINTESI*\n{summary}",
    ]

    if salient_points:
        sections.append(
            f"🔑 *PUNTI SALIENTI*\n{salient_points}"
        )

    if important_details:
        sections.append(
            f"🗓️ *DETTAGLI IMPORTANTI*\n{important_details}"
        )

    language = str(api_data.get("language") or "").strip().lower()

    if language and language not in ("it", "italian", "italiano"):
        # Manteniamo l'indicazione della lingua straniera del vecchio bot.
        sections.insert(
            1,
            f"🌍 Vocale in lingua: {language}"
        )

    return "\n\n".join(sections)


def send_whatsapp_message(phone_id, token, recipient, text):
    """Invia un messaggio WhatsApp tramite Meta."""
    url = (
        f"https://graph.facebook.com/"
        f"{META_GRAPH_VERSION}/{phone_id}/messages"
    )

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "messaging_product": "whatsapp",
            "to": recipient,
            "text": {
                "body": text[:4000]
            },
        },
        timeout=META_TIMEOUT,
    )

    log(
        f"Invio WhatsApp: HTTP {response.status_code}"
    )

    response.raise_for_status()


def download_whatsapp_audio(audio_id, token):
    """Recupera il vocale da Meta e lo mantiene in memoria."""
    media_info_url = (
        f"https://graph.facebook.com/"
        f"{META_GRAPH_VERSION}/{audio_id}"
    )

    media_info_response = requests.get(
        media_info_url,
        headers={
            "Authorization": f"Bearer {token}"
        },
        timeout=META_TIMEOUT,
    )

    media_info_response.raise_for_status()

    media_info = media_info_response.json()
    media_url = media_info.get("url")

    if not media_url:
        raise ValueError("Meta non ha restituito l'URL del vocale")

    audio_response = requests.get(
        media_url,
        headers={
            "Authorization": f"Bearer {token}"
        },
        timeout=META_TIMEOUT,
    )

    audio_response.raise_for_status()

    audio_bytes = audio_response.content

    if not audio_bytes:
        raise ValueError("Il file audio scaricato è vuoto")

    mime_type = str(
        media_info.get("mime_type") or "audio/ogg"
    ).lower()

    # La nostra API accetta multipart/form-data.
    # Il nome del file deve avere un'estensione riconosciuta da OpenAI.
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
        f"Audio scaricato: {len(audio_bytes)} bytes; "
        f"formato: {filename}"
    )

    return audio_bytes, filename, upload_mime


def process_audio_with_vocalflash(audio_bytes, filename, mime_type, api_key):
    """Invia il vocale al nostro motore API, senza chiamare OpenAI da Render."""
    response = requests.post(
        VOCALFLASH_API_URL,
        headers={
            "X-API-Key": api_key
        },
        files={
            "file": (
                filename,
                audio_bytes,
                mime_type
            )
        },
        timeout=VOCALFLASH_API_TIMEOUT,
    )

    log(
        f"API VocalFlash: HTTP {response.status_code}"
    )

    response.raise_for_status()

    api_data = response.json()

    if api_data.get("ok") is not True:
        raise ValueError(
            "L'API VocalFlash non ha completato l'elaborazione"
        )

    log("Sintesi ricevuta correttamente dalla nostra API")

    return build_whatsapp_response(api_data)


@app.route("/", methods=["GET", "POST"])
@app.route("/whatsapp", methods=["GET", "POST"])
@app.route("/webhook", methods=["GET", "POST"])
def whatsapp():

    log(
        f">>> RICHIESTA: {request.method} {request.path}"
    )

    # --------------------------------------------------
    # VERIFICA WEBHOOK META
    # --------------------------------------------------

    if request.method == "GET":

        if "hub.challenge" not in request.args:
            return "VocalFlash bot is running!"

        verify_token = os.getenv("WA_VERIFY_TOKEN", "").strip()

        if not verify_token:
            log("WA_VERIFY_TOKEN non configurato")
            return "error", 500

        if request.args.get("hub.verify_token") == verify_token:

            log("WEBHOOK VERIFICATO")

            return request.args.get("hub.challenge")

        log("VERIFY FALLITO")

        return "error", 403

    # --------------------------------------------------
    # RICEZIONE MESSAGGIO WHATSAPP
    # --------------------------------------------------

    log(
        f">>> POST ARRIVATO - content-type: {request.content_type}"
    )

    if not request.data:
        log(">>> BODY VUOTO")
        return "ok", 200

    try:

        data = request.get_json(silent=True)

        if not data:
            log(">>> JSON NON VALIDO")
            return "ok", 200

        # Riga di debug richiesta, conservata.
        print(f"Dati: {data}", flush=True)

        entries = data.get("entry") or []

        if not entries:
            log("Nessuna entry nel webhook")
            return "ok", 200

        config = get_config()

        if not config["wa_token"]:
            raise ValueError("WA_TOKEN non configurato")

        if not config["api_key"]:
            raise ValueError(
                "VOCALFLASH_INTERNAL_API_KEY non configurata"
            )

        for entry in entries:

            for change in entry.get("changes") or []:

                value = change.get("value") or {}
                messages = value.get("messages") or []

                if not messages:
                    log("Nessun messaggio nel webhook")
                    continue

                for msg in messages:

                    if msg.get("type") != "audio" or "audio" not in msg:
                        log(
                            f"Messaggio non audio: {msg.get('type')}"
                        )
                        continue

                    from_id = msg.get("from")
                    audio_id = (msg.get("audio") or {}).get("id")

                    if not from_id or not audio_id:
                        log("Messaggio audio incompleto")
                        continue

                    log("Recupero audio da Meta...")

                    audio_bytes, filename, mime_type = (
                        download_whatsapp_audio(
                            audio_id,
                            config["wa_token"]
                        )
                    )

                    log("Invio audio alla nostra API VocalFlash...")

                    try:

                        testo_risposta = process_audio_with_vocalflash(
                            audio_bytes,
                            filename,
                            mime_type,
                            config["api_key"]
                        )

                    except Exception:

                        log("Errore durante l'elaborazione API VocalFlash")
                        traceback.print_exc()

                        testo_risposta = (
                            "⚡ *VocalFlash*\n\n"
                            "Non sono riuscito a elaborare questo vocale. "
                            "Riprova tra poco."
                        )

                    send_whatsapp_message(
                        config["phone_id"],
                        config["wa_token"],
                        from_id,
                        testo_risposta
                    )

    except Exception as e:

        log(f"ERRORE: {e}")
        traceback.print_exc()

    return "ok", 200


# --------------------------------------------------
# PRIVACY
# --------------------------------------------------

@app.route("/privacy")
def privacy():

    return (
        "<h1>Privacy VocalFlash 08/09/2026</h1>"
        "Audio elaborato tramite l'infrastruttura VocalFlash "
        "e inviato ai servizi necessari alla trascrizione e sintesi. "
        "Contatto: reddyanastasi@hotmail.it",
        200
    )


# --------------------------------------------------
# AVVIO APP
# --------------------------------------------------

if __name__ == "__main__":

    port = int(
        os.getenv("PORT", 10000)
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
