```python
from flask import Flask, request
import requests, os, traceback
from openai import OpenAI

app = Flask(__name__)

@app.route('/', methods=["GET", "POST"])
@app.route("/whatsapp", methods=["GET", "POST"])
@app.route("/webhook", methods=["GET", "POST"])
def whatsapp():

    print(
        f">>> RICHIESTA: {request.method} {request.path}",
        flush=True
    )

    if request.method == "GET":
        if "hub.challenge" not in request.args:
            return 'VocalFlash bot is running!'

        verify_token = (
            os.getenv("WA_VERIFY_TOKEN")
            or os.getenv("VERIFY_TOKEN")
            or "ciao123"
        )

        if request.args.get("hub.verify_token") == verify_token:
            print(
                f"WEBHOOK VERIFICATO con token {verify_token}",
                flush=True
            )
            return request.args.get("hub.challenge")

        print(
            f"VERIFY FALLITO: ricevuto "
            f"{request.args.get('hub.verify_token')} "
            f"atteso {verify_token}",
            flush=True
        )
        return "error", 403

    print(
        f">>> POST ARRIVATO - content-type: {request.content_type}",
        flush=True
    )

    if not request.data:
        print(">>> BODY VUOTO", flush=True)
        return "ok", 200

    try:
        data = request.get_json(silent=True)

        if not data:
            print(">>> JSON NON VALIDO", flush=True)
            return "ok", 200

        print(f"Dati: {data}", flush=True)

        value = data['entry'][0]['changes'][0]['value']

        if 'messages' not in value:
            print("Nessun messages nel value", flush=True)
            return "ok", 200

        msg = value['messages'][0]
        from_id = msg['from']

        if 'audio' not in msg:
            print(
                f"Messaggio non audio: {msg.get('type')}",
                flush=True
            )
            return "ok", 200

        audio_id = msg['audio']['id']

        token = os.getenv("WA_TOKEN")

        phone_id = (
            os.getenv("WA_PHONE_ID")
            or os.getenv("PHONE_NUMBER_ID")
            or "1318571384677144"
        )

        print(
            f"Recupero url per {audio_id}",
            flush=True
        )

        r = requests.get(
            f"https://graph.facebook.com/v20.0/{audio_id}",
            headers={
                "Authorization": f"Bearer {token}"
            }
        )

        print(
            f"Media info: {r.text}",
            flush=True
        )

        r.raise_for_status()

        media_url = r.json()['url']

        print(
            "Scarico audio...",
            flush=True
        )

        audio_resp = requests.get(
            media_url,
            headers={
                "Authorization": f"Bearer {token}"
            }
        )

        audio_resp.raise_for_status()

        with open("/tmp/audio.ogg", "wb") as f:
            f.write(audio_resp.content)

        print(
            f"Audio {len(audio_resp.content)} bytes salvato",
            flush=True
        )

        openai_key = (
            os.getenv("OPENAI_API_KEY")
            or os.getenv("OPENAI_KEY")
        )

        if not openai_key or not openai_key.startswith("sk-"):
            testo_risposta = (
                f"✅ Vocale ricevuto "
                f"({len(audio_resp.content)} bytes)! "
                f"Metti OPENAI_KEY vera su Render "
                f"per trascrizione."
            )

        else:
            client = OpenAI(
                api_key=openai_key
            )

            with open("/tmp/audio.ogg", "rb") as f:
                tr = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="verbose_json"
                )

                transcript = tr.text
                lingua = tr.language

            print(
                f"Trascrizione: {transcript} [{lingua}]",
                flush=True
            )

            if lingua != "it":

                trad = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "system",
                            "content": "Traduci in italiano naturale"
                        },
                        {
                            "role": "user",
                            "content": transcript
                        }
                    ]
                ).choices[0].message.content

                testo_risposta = (
                    f"🌍 {lingua}->IT\n"
                    f"🎤 {transcript}\n"
                    f"🇮🇹 {trad}"
                )

            else:

                riass = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                f"Riassumi in 3 punti: {transcript}"
                            )
                        }
                    ]
                ).choices[0].message.content

                testo_risposta = (
                    f"🎤 {transcript}\n\n"
                    f"📌 {riass}"
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
                "text": {
                    "body": testo_risposta[:4000]
                }
            }
        )

        print(
            f"Risposta: {resp.status_code} {resp.text}",
            flush=True
        )

    except Exception as e:
        print(
            f"ERRORE: {e}",
            flush=True
        )
        traceback.print_exc()

    return "ok", 200


@app.route('/privacy')
def privacy():
    return (
        "<h1>Privacy VocalFlash 08/09/2026</h1>"
        "Audio temporaneo in /tmp, inviato a OpenAI, "
        "nessuna vendita dati. "
        "Contatto: reddyanastasi@hotmail.it",
        200
    )


if __name__ == "__main__":
    port = int(
        os.getenv("PORT", 10000)
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
```
