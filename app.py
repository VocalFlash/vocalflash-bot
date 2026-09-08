from flask import Flask, request
import requests, os, traceback
from openai import OpenAI

app = Flask(__name__)

@app.route('/', methods=["GET", "POST"])
@app.route("/whatsapp", methods=["GET", "POST"])
@app.route("/webhook", methods=["GET", "POST"])
def whatsapp():
    if request.method == "GET":
        if "hub.challenge" not in request.args:
            return 'VocalFlash bot is running!'
        if request.args.get("hub.verify_token") == os.getenv("WA_VERIFY_TOKEN"):
            return request.args.get("hub.challenge")
        return "error", 403

    if not request.data:
        return "ok", 200
    try:
        data = request.get_json(silent=True)
        if not data: return "ok", 200
        print(f"Dati: {data}")

        value = data['entry'][0]['changes'][0]['value']
        if 'messages' not in value: return "ok", 200
        msg = value['messages'][0]
        from_id = msg['from']
        if 'audio' not in msg: return "ok", 200

        audio_id = msg['audio']['id']
        token = os.getenv("WA_TOKEN")
        phone_id = os.getenv("WA_PHONE_ID")

        # FIX: chiedi SEMPRE url fresco a Graph, non usare quello del payload
        print(f"Recupero url per {audio_id}")
        r = requests.get(f"https://graph.facebook.com/v20.0/{audio_id}", headers={"Authorization": f"Bearer {token}"})
        print(f"Media info: {r.text}")
        r.raise_for_status()
        media_url = r.json()['url']

        print(f"Scarico audio...")
        audio_resp = requests.get(media_url, headers={"Authorization": f"Bearer {token}"})
        audio_resp.raise_for_status()
        with open("/tmp/audio.ogg","wb") as f: f.write(audio_resp.content)
        print(f"Audio {len(audio_resp.content)} bytes salvato")

        openai_key = os.getenv("OPENAI_KEY")
        if not openai_key or not openai_key.startswith("sk-"):
            testo_risposta = f"✅ Vocale ricevuto ({len(audio_resp.content)} bytes)! Metti OPENAI_KEY vera su Render per trascrizione."
        else:
            client = OpenAI(api_key=openai_key)
            with open("/tmp/audio.ogg","rb") as f:
                tr = client.audio.transcriptions.create(model="whisper-1", file=f, response_format="verbose_json")
                transcript = tr.text
                lingua = tr.language
            print(f"Trascrizione: {transcript} [{lingua}]")
            if lingua!= "it":
                trad = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"system","content":"Traduci in italiano naturale"},{"role":"user","content":f"{transcript}"}]).choices[0].message.content
                testo_risposta = f"🌍 {lingua}->IT\n🎤 {transcript}\n🇮🇹 {trad}"
            else:
                riass = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"user","content":f"Riassumi in 3 punti: {transcript}"}]).choices[0].message.content
                testo_risposta = f"🎤 {transcript}\n\n📌 {riass}"

        resp = requests.post(f"https://graph.facebook.com/v20.0/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type":"application/json"},
            json={"messaging_product":"whatsapp","to":from_id,"text":{"body":testo_risposta[:4000]}})
        print(f"Risposta: {resp.status_code} {resp.text}")

    except Exception as e:
        print(f"ERRORE: {e}")
        traceback.print_exc()
    return "ok", 200

@app.route('/privacy')
def privacy():
    return "<h1>Privacy VocalFlash 08/09/2026</h1>Audio temporaneo in /tmp, inviato a OpenAI, nessuna vendita dati. Contatto: reddyanastasi@hotmail.it", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
