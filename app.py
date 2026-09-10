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

    # --------------------------------------------------
    # VERIFICA WEBHOOK META
    # --------------------------------------------------

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


    # --------------------------------------------------
    # RICEZIONE MESSAGGIO WHATSAPP
    # --------------------------------------------------

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

            print(
                "Nessun messages nel value",
                flush=True
            )

            return "ok", 200


        msg = value['messages'][0]

        from_id = msg['from']


        # --------------------------------------------------
        # ACCETTIAMO SOLO MESSAGGI AUDIO
        # --------------------------------------------------

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


        # --------------------------------------------------
        # RECUPERO URL AUDIO DA META
        # --------------------------------------------------

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


        # --------------------------------------------------
        # DOWNLOAD AUDIO
        # --------------------------------------------------

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


        # --------------------------------------------------
        # OPENAI
        # --------------------------------------------------

        openai_key = (
            os.getenv("OPENAI_API_KEY")
            or os.getenv("OPENAI_KEY")
        )


        if not openai_key or not openai_key.startswith("sk-"):

            testo_risposta = (
                f"✅ Vocale ricevuto "
                f"({len(audio_resp.content)} bytes)! "
                f"Configurare OPENAI_API_KEY "
                f"per elaborare il messaggio."
            )


        else:

            client = OpenAI(
                api_key=openai_key
            )


            # --------------------------------------------------
            # TRASCRIZIONE INTERNA DEL VOCALE
            # La trascrizione NON viene inviata all'utente
            # --------------------------------------------------

            with open("/tmp/audio.ogg", "rb") as f:

                tr = client.audio.transcriptions.create(

                    model="whisper-1",

                    file=f,

                    response_format="verbose_json"

                )


            transcript = tr.text

            lingua = tr.language


            print(
                f"Trascrizione interna: {transcript} [{lingua}]",
                flush=True
            )


            # --------------------------------------------------
            # MOTORE DI SINTESI VOCALFLASH
            # --------------------------------------------------

            sintesi = client.chat.completions.create(

                model="gpt-4o-mini",

                messages=[

                    {
                        "role": "system",

                        "content": """
Sei il motore di sintesi intelligente di VocalFlash.

VocalFlash serve a permettere all'utente di capire rapidamente
un lungo messaggio vocale senza doverlo ascoltare o leggere
integralmente.

Riceverai la trascrizione automatica di un messaggio vocale.

Devi comprenderne il significato ed estrarre ESCLUSIVAMENTE
le informazioni realmente utili.

REGOLE IMPORTANTI:

- NON riportare la trascrizione completa.
- NON riscrivere frase per frase il messaggio.
- NON limitarti a tagliare il testo originale.
- Elimina saluti, convenevoli, esitazioni, ripetizioni,
  intercalari e divagazioni.
- Riassumi il significato, non le singole frasi.
- Individua il punto centrale del messaggio.
- Evidenzia decisioni, richieste e conclusioni.
- Individua eventuali azioni che il destinatario deve compiere.
- Conserva date, orari, luoghi, nomi, cifre, importi,
  appuntamenti e scadenze quando sono importanti.
- Non inventare mai informazioni.
- Se una informazione non è presente, non aggiungerla.
- Se il vocale è molto breve, mantieni anche la sintesi molto breve.
- Se il vocale è lungo o complesso, organizza le informazioni
  in modo chiaro e facilmente leggibile.
- Rispondi SEMPRE in italiano, anche se il messaggio originale
  è in un'altra lingua.

USA QUESTO FORMATO:

📌 *IN SINTESI*
Una sintesi breve e naturale che permetta di capire subito
il contenuto principale del vocale.

🔑 *PUNTI SALIENTI*
• Inserisci soltanto le informazioni importanti.
• Usa pochi punti chiari.
• Non ripetere ciò che hai già scritto inutilmente.

✅ *DA FARE*
Inserisci questa sezione SOLTANTO se nel messaggio esistono
azioni, richieste, compiti o decisioni che richiedono
un comportamento concreto.

🗓️ *DETTAGLI IMPORTANTI*
Inserisci questa sezione SOLTANTO se sono presenti elementi
come date, orari, appuntamenti, luoghi, nomi, importi,
numeri o scadenze che vale la pena ricordare.

L'obiettivo principale è far risparmiare tempo all'utente.

La risposta deve contenere l'essenziale, non una trascrizione.
"""
                    },

                    {
                        "role": "user",
                        "content": transcript
                    }

                ]

            ).choices[0].message.content


            print(
                f"Sintesi VocalFlash: {sintesi}",
                flush=True
            )


            # --------------------------------------------------
            # INDICAZIONE LINGUA STRANIERA
            # --------------------------------------------------

            lingua_normalizzata = str(lingua).lower()

            lingue_italiane = [
                "it",
                "italian",
                "italiano"
            ]


            if lingua_normalizzata not in lingue_italiane:

                testo_risposta = (
                    f"⚡ *VocalFlash*\n"
                    f"🌍 Vocale in lingua: {lingua}\n\n"
                    f"{sintesi}"
                )

            else:

                testo_risposta = (
                    f"⚡ *VocalFlash*\n\n"
                    f"{sintesi}"
                )


        # --------------------------------------------------
        # INVIO RISPOSTA SU WHATSAPP
        # --------------------------------------------------

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


# --------------------------------------------------
# PRIVACY
# --------------------------------------------------

@app.route('/privacy')
def privacy():

    return (

        "<h1>Privacy VocalFlash 08/09/2026</h1>"
        "Audio temporaneo in /tmp, inviato a OpenAI, "
        "nessuna vendita dati. "
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
