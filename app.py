from flask import Flask, request
import requests, os
from openai import OpenAI

app = Flask(__name__)

@app.route('/')
def home():
    return 'VocalFlash bot is running!'

@app.route("/whatsapp", methods=["GET", "POST"])
def whatsapp():
    if request.method == "GET":
        print("GET verifica ricevuta")
        if request.args.get("hub.verify_token") == os.getenv("WA_VERIFY_TOKEN"):
            print("Verifica OK")
            return request.args.get("hub.challenge")
        print("Verifica FALLITA - token sbagliato")
        return "error", 403

    # POST - arriva il vocale
    print("POST /whatsapp ARRIVATO!", request.get_json())
    try:
        data = request.get_json()
        value = data['entry'][0]['changes'][0]['value']
        if 'messages' not in value:
            print("Niente messages, è uno stato - ignoro")
            return "ok", 200

        msg = value['messages'][0]
        from_id = msg['from']
        if 'audio' not in msg:
            print(f"Messaggio non è audio: {msg}")
            return "ok", 200

        audio_id = msg['audio']['id']
        print(f"Audio ID: {audio_id} da {from_id}")

        token = os.getenv("WA_TOKEN")
        r = requests.get(f"https://graph.facebook.com/v20.0/{audio_id}", headers={"Authorization": f"Bearer {token}"})
        print(f"Meta media info: {r.text}")
        media_url = r.json()['url']

        audio_data = requests.get(media_url, headers={"Authorization": f"Bearer {token}"}).content
        with open("/tmp/audio.ogg","wb") as f:
            f.write(audio_data)
        print("Audio scaricato")

        openai_key = os.getenv("OPENAI_KEY")
        if not openai_key or openai_key == "temp" or not openai_key.startswith("sk-"):
            print("OPENAI_KEY finta, mando messaggio di test")
            testo_risposta = f"✅ Ho ricevuto il tuo vocale! (ID: {audio_id}) - Metti la chiave OpenAI vera su Render per avere trascrizione e traduzione."
        else:
            client = OpenAI(api_key=openai_key)
            with open("/tmp/audio.ogg","rb") as f:
                tr = client.audio.transcriptions.create(model="whisper-1", file=f, response_format="verbose_json")
                transcript = tr.text
                lingua = tr.language
                print(f"Trascrizione: {transcript} | Lingua: {lingua}")

            # LOGICA TRADUZIONE CONTESTUALE
            if lingua!= "it":
                traduzione = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "Sei un traduttore. Traduci in italiano naturale mantenendo tono, contesto e slang."},
                        {"role": "user", "content": f"Traduci da {lingua} a italiano: {transcript}"}
                    ]
                ).choices[0].message.content

                riassunto = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": f"Riassumi in 3 punti in italiano, usa la traduzione per capire il contesto. Originale ({lingua}): {transcript} Tradotto: {traduzione}"}]
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
        print(f"Risposta inviata: {resp.text}")

    except Exception as e:
        print(f"ERRORE: {e}")
        import traceback
        traceback.print_exc()
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
