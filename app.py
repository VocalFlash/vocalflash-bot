from flask import Flask, request
import requests, os, traceback
from openai import OpenAI

app = Flask(__name__)

@app.route('/', methods=["GET", "POST"])
@app.route("/whatsapp", methods=["GET", "POST"])
@app.route("/webhook", methods=["GET", "POST"])
def whatsapp():
    if request.method == "GET":
        print("GET verifica ricevuta")
        if "hub.challenge" not in request.args:
            return 'VocalFlash bot is running!'
        if request.args.get("hub.verify_token") == os.getenv("WA_VERIFY_TOKEN"):
            print("Verifica OK")
            return request.args.get("hub.challenge")
        print("Verifica FALLITA")
        return "error", 403

    print(f"--- POST {request.path} ARRIVATO ---")
    # FIX CRASH: se il body è vuoto (controllo di Meta/Render) ignora
    if not request.data:
        print("POST vuoto, ignoro")
        return "ok", 200

    try:
        data = request.get_json(silent=True)
        if not data:
            print("JSON vuoto o non valido, ignoro")
            return "ok", 200
        print(f"Dati: {data}")
        value = data['entry'][0]['changes'][0]['value']
        if 'messages' not in value:
            print("Stato, non messaggio - ignoro")
            return "ok", 200

        msg = value['messages'][0]
        from_id = msg['from']
        if 'audio' not in msg:
            print(f"Non audio: {msg.get('type')}")
            return "ok", 200

        audio_id = msg['audio']['id']
        media_url = msg['audio'].get('url')
        token = os.getenv("WA_TOKEN")

        if not media_url:
            print(f"Recupero url per audio {audio_id}")
            r = requests.get(f"https://graph.facebook.com/v20.0/{audio_id}", headers={"Authorization": f"Bearer {token}"})
            print(f"Media info: {r.status_code} {r.text}")
            r.raise_for_status()
            media_url = r.json()['url']

        print(f"Scarico audio...")
        audio_resp = requests.get(media_url, headers={"Authorization": f"Bearer {token}"})
        audio_resp.raise_for_status()
        with open("/tmp/audio.ogg","wb") as f:
            f.write(audio_resp.content)
        print(f"Audio salvato: {len(audio_resp.content)} bytes")

        openai_key = os.getenv("OPENAI_KEY")
        if not openai_key or openai_key == "temp" or not openai_key.startswith("sk-"):
            print("OPENAI_KEY finta")
            testo_risposta = f"✅ Vocale ricevuto! (ID: {audio_id}) - Metti la chiave OpenAI vera su Render che inizi con sk- per avere trascrizione."
        else:
            client = OpenAI(api_key=openai_key)
            with open("/tmp/audio.ogg","rb") as f:
                tr = client.audio.transcriptions.create(model="whisper-1", file=f, response_format="verbose_json")
                transcript = tr.text
                lingua = tr.language
                print(f"Trascrizione: {transcript} | Lingua: {lingua}")

            if lingua!= "it":
                traduzione = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "Sei un traduttore. Traduci in italiano naturale mantenendo tono e slang."},
                        {"role": "user", "content": f"Traduci da {lingua} a italiano: {transcript}"}
                    ]
                ).choices[0].message.content
                riassunto = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": f"Riassumi in 3 punti in italiano. Originale ({lingua}): {transcript} Tradotto: {traduzione}"}]
                ).choices[0].message.content
                testo_risposta = f"🌍 Rilevato: {lingua} -> IT\n🎤 Originale: {transcript}\n🇮🇹 Tradotto: {traduzione}\n\n📌 Riassunto:\n{riassunto}"
            else:
                riassunto = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": f"Riassumi in 3 punti: {transcript}"}]
                ).choices[0].message.content
                testo_risposta = f"🎤 {transcript}\n\n📌 Riassunto:\n{riassunto}"

        phone_id = os.getenv("WA_PHONE_ID")
        resp = requests.post(f"https://graph.facebook.com/v20.0/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"messaging_product":"whatsapp","to":from_id,"text":{"body":testo_risposta}})
        print(f"Risposta inviata: {resp.status_code} {resp.text}")
        resp.raise_for_status()

    except Exception as e:
        print(f"ERRORE: {e}")
        traceback.print_exc()
    return "ok", 200

@app.route('/privacy')
def privacy():
    html = """
    <html><head><meta charset="utf-8"><title>Privacy Policy - VocalFlash</title></head>
    <body style="font-family:sans-serif;max-width:800px;margin:40px auto;padding:20px;line-height:1.6">
    <h1>Privacy Policy - VocalFlash</h1>
    <p><strong>Ultimo aggiornamento: 08/09/2026</strong></p>
    <p>VocalFlash trascrive vocali WhatsApp in testo con traduzione e riassunto.</p>
    <h3>1. Dati</h3><p>ID audio e numero mittente via WhatsApp API. Audio scaricato in /tmp temporaneamente.</p>
    <h3>2. Uso</h3><p>Audio inviato a OpenAI Whisper per trascrizione e GPT per traduzione. Nessun salvataggio permanente.</p>
    <h3>3. Conservazione</h3><p>File in /tmp sovrascritti e cancellati al riavvio. Nessun database.</p>
    <h3>4. Condivisione</h3><p>Solo Meta (risposta) e OpenAI (trascrizione). Nessuna vendita dati.</p>
    <h3>5. Contatti</h3><p>reddyanastasi@hotmail.it</p>
    </body></html>
    """
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
