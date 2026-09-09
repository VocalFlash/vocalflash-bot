```python
from flask import Flask, request
import requests
import os
import traceback
from openai import OpenAI

app = Flask(__name__)


@app.route("/", methods=["GET", "POST"])
@app.route("/whatsapp", methods=["GET", "POST"])
@app.route("/webhook", methods=["GET", "POST"])
def whatsapp():

    # LOG IMMEDIATO DI QUALSIASI RICHIESTA
    print(
        f">>> RICHIESTA RICEVUTA: "
        f"method={request.method} "
        f"path={request.path} "
        f"content_type={request.content_type} "
        f"content_length={request.content_length}",
        flush=True
    )

    # =========================
    # VERIFICA WEBHOOK META
    # =========================
    if request.method == "GET":

        # Se non è una verifica Meta, mostra stato bot
        if "hub.challenge" not in request.args:
            return "VocalFlash bot is running!", 200

        verify_token = (
            os.getenv("WA_VERIFY_TOKEN")
            or os.getenv("VERIFY_TOKEN")
            or "ciao123"
        )

        received_token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if received_token == verify_token:
            print(
                f">>> WEBHOOK VERIFICATO con token {verify_token}",
                flush=True
            )
            return challenge, 200

        print(
            f">>> VERIFY FALLITO: "
            f"ricevuto={received_token} "
            f"atteso={verify_token}",
            flush=True
        )

        return "error", 403

    # =========================
    # POST WHATSAPP
    # =========================

    print(
        f">>> POST BODY RAW: {request.data[:1000]!r}",
        flush=True
    )

    if not request.data:
        print(
            ">>> POST ARRIVATO MA BODY VUOTO",
            flush=True
        )
        return "ok", 200

    try:

        data = request.get_json(silent=True)

        if not data:
            print(
                ">>> POST ARRIVATO MA JSON NON VALIDO",
                flush=True
            )
            return "ok", 200

        print(
            f">>> JSON RICEVUTO: {data}",
            flush=True
        )

        # =========================
        # LETTURA PAYLOAD META
        # =========================

        entry = data.get("entry")

        if not entry:
            print(
                ">>> Nessun 'entry' nel payload",
                flush=True
            )
            return "ok", 200

        changes = entry[0].get("changes")

        if not changes:
            print(
                ">>> Nessun 'changes' nel payload",
                flush=True
            )
            return "ok", 200

        value = changes[0].get("value", {})

        if "messages" not in value:
            print(
                ">>> Nessun 'messages' nel value",
                flush=True
            )
            return "ok", 200

        messages = value.get("messages", [])

        if not messages:
            print(
                ">>> Lista messages vuota",
                flush=True
            )
            return "ok", 200

        msg = messages[0]

        print(
            f">>> TIPO MESSAGGIO: {msg.get('type')}",
            flush=True
        )

        from_id = msg.get("from")

        if not from_id:
            print(
                ">>> Mittente non trovato",
                flush=True
            )
            return "ok", 200

        # =========================
        # CONTROLLO AUDIO
        # =========================

        if msg.get("type") != "audio" or "audio" not in msg:
            print(
                f">>> Messaggio non audio: {msg.get('type')}",
                flush=True
            )
            return "ok", 200

        audio_id = msg["audio"].get("id")

        if not audio_id:
            print(
                ">>> Audio ID non trovato",
                flush=True
            )
            return "ok", 200

        print(
            f">>> AUDIO RICEVUTO. ID={audio_id}",
            flush=True
        )

        # =========================
        # VARIABILI META
        # =========================

        token = os.getenv("WA_TOKEN")

        phone_id = (
            os.getenv("WA_PHONE_ID")
            or os.getenv("PHONE_NUMBER_ID")
            or "1318571384677144"
        )

        if not token:
            print(
                ">>> ERRORE: WA_TOKEN non presente su Render",
                flush=True
            )
            return "ok", 200

        # =========================
        # RECUPERO URL AUDIO
        # =========================

        print(
            f">>> Recupero URL media per audio_id={audio_id}",
            flush=True
        )

        media_info = requests.get(
            f"https://graph.facebook.com/v20.0/{audio_id}",
            headers={
                "Authorization": f"Bearer {token}"
            },
            timeout=30
        )

        print(
            f">>> MEDIA INFO STATUS: {media_info.status_code}",
            flush=True
        )

        print(
            f">>> MEDIA INFO BODY: {media_info.text}",
            flush=True
        )

        media_info.raise_for_status()

        media_json = media_info.json()

        media_url = media_json.get("url")

        if not media_url:
            print(
                ">>> URL media non trovato nella risposta Meta",
                flush=True
            )
            return "ok", 200

        # =========================
        # DOWNLOAD AUDIO
        # =========================

        print(
            ">>> Download audio...",
            flush=True
        )

        audio_resp = requests.get(
            media_url,
            headers={
                "Authorization": f"Bearer {token}"
            },
            timeout=60
        )

        print(
            f">>> AUDIO DOWNLOAD STATUS: {audio_resp.status_code}",
            flush=True
        )

        audio_resp.raise_for_status()

        audio_path = "/tmp/audio.ogg"

        with open(audio_path, "wb") as f:
            f.write(audio_resp.content)

        print(
            f">>> AUDIO SALVATO: {len(audio_resp.content)} bytes",
            flush=True
        )

        # =========================
        # OPENAI
        # =========================

        openai_key = (
            os.getenv("OPENAI_API_KEY")
            or os.getenv("OPENAI_KEY")
        )

        if not openai_key:
            print(
                ">>> OPENAI KEY ASSENTE",
                flush=True
            )

            testo_risposta = (
                f"✅ Vocale ricevuto correttamente "
                f"({len(audio_resp.content)} bytes).\n"
                f"La chiave OpenAI non è configurata."
            )

        else:

            print(
                ">>> Avvio trascrizione OpenAI...",
                flush=True
            )

            client = OpenAI(
                api_key=openai_key
            )

            with open(audio_path, "rb") as f:
                tr = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="verbose_json"
                )

            transcript = tr.text
            lingua = getattr(tr, "language", "it")

            print(
                f">>> TRASCRIZIONE: {transcript}",
                flush=True
            )

            print(
                f">>> LINGUA: {lingua}",
                flush=True
            )

            # =========================
            # TRADUZIONE / RIASSUNTO
            # =========================

            if lingua != "it":

                print(
                    ">>> Traduco in italiano...",
                    flush=True
                )

                traduzione = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "system",
                            "content": "Traduci in italiano naturale."
                        },
                        {
                            "role": "user",
                            "content": transcript
                        }
                    ]
                )

                trad = traduzione.choices[0].message.content

                testo_risposta = (
                    f"🌍 Lingua: {lingua}\n\n"
                    f"🎤 Trascrizione:\n{transcript}\n\n"
                    f"🇮🇹 Traduzione:\n{trad}"
                )

            else:

                print(
                    ">>> Creo riassunto...",
                    flush=True
                )

                riassunto = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Riassumi il messaggio vocale "
                                "in italiano in 3 punti chiari e sintetici."
                            )
                        },
                        {
                            "role": "user",
                            "content": transcript
                        }
                    ]
                )

                riass = riassunto.choices[0].message.content

                testo_risposta = (
                    f"🎤 Trascrizione:\n{transcript}\n\n"
                    f"📌 Sintesi:\n{riass}"
                )

        # =========================
        # RISPOSTA WHATSAPP
        # =========================

        print(
            f">>> Invio risposta WhatsApp a {from_id}",
            flush=True
        )

        resp = requests.post(
            f"https://graph.facebook.com/v20.0/{phone_id}/messages",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json={
                "messaging_product": "whatsapp",
                "to": from_id,
                "type": "text",
                "text": {
                    "body": testo_risposta[:4000]
                }
            },
            timeout=30
        )

        print(
            f">>> RISPOSTA WHATSAPP STATUS: {resp.status_code}",
            flush=True
        )

        print(
            f">>> RISPOSTA WHATSAPP BODY: {resp.text}",
            flush=True
        )

    except Exception as e:

        print(
            f">>> ERRORE GENERALE: {e}",
            flush=True
        )

        traceback.print_exc()

    return "ok", 200


# =========================
# PRIVACY
# =========================

@app.route("/privacy")
def privacy():

    return """
    <h1>Privacy VocalFlash</h1>
    <p>
    I messaggi audio vengono elaborati temporaneamente
    per effettuare trascrizione e sintesi.
    </p>
    <p>
    Gli audio vengono salvati temporaneamente nella cartella /tmp
    del server e possono essere inviati a OpenAI per l'elaborazione.
    </p>
    <p>
    Nessuna vendita dei dati.
    </p>
    <p>
    Contatto: reddyanastasi@hotmail.it
    </p>
    """, 200


# =========================
# AVVIO SERVER
# =========================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            10000
        )
    )

    print(
        f">>> VocalFlash avviato sulla porta {port}",
        flush=True
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
```
