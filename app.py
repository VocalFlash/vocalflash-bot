from flask import Flask, request
import requests, os
from openai import OpenAI

app = Flask(__name__)

@app.route('/')
def home():
    return 'VocalFlash bot is running! Use /whatsapp for webhook'

@app.route("/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == os.getenv("WA_VERIFY_TOKEN"):
        return request.args.get("hub.challenge")
    return "error", 403

@app.route("/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    try:
        msg = data['entry'][0]['changes'][0]['value']['messages'][0]
        from_id = msg['from']
        audio_id = msg['audio']['id']

        token = os.getenv("WA_TOKEN")
        url = f"https://graph.facebook.com/v20.0/{audio_id}"
        media_url = requests.get(url, headers={"Authorization": f"Bearer {token}"}).json()['url']
        audio_data = requests.get(media_url, headers={"Authorization": f"Bearer {token}"}).content

        with open("/tmp/audio.ogg","wb") as f: f.write(audio_data)

        # se OPENAI_KEY è temp, non rompere il deploy
        openai_key = os.getenv("OPENAI_KEY")
        if not openai_key or openai_key == "temp" or not openai_key.startswith("sk-"):
            print("OPENAI_KEY mancante o finta")
            return "ok", 200

        client = OpenAI(api_key=openai_key)

        with open("/tmp/audio.ogg","rb") as f:
            transcript = client.audio.transcriptions.create(model="whisper-1", file=f).text

        summary = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"user","content":f"Riassumi questo vocale in 3 punti: {transcript}"}]).choices[0].message.content

        phone_id = os.getenv("WA_PHONE_ID")
        requests.post(f"https://graph.facebook.com/v20.0/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"messaging_product":"whatsapp","to":from_id,"text":{"body":summary}})
    except Exception as e:
        print(e)
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
