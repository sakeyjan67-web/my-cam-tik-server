import os
import requests
import firebase_admin
from firebase_admin import credentials, firestore
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# =========================
# ENV VARIABLES
# =========================

TOKEN = os.getenv("8894560001:AAGwZm-jID0rgDdpD3DX9HZGcubbtTOeqr8")
CHANNEL_ID = os.getenv("-1004318297083")

if not TOKEN:
    print("ERROR: BOT_TOKEN missing")

if not CHANNEL_ID:
    print("ERROR: CHANNEL_ID missing")


# =========================
# FIREBASE
# =========================

firebase_json = os.getenv("FIREBASE_CREDENTIALS")

try:
    if firebase_json:
        import json
        cred = credentials.Certificate(json.loads(firebase_json))
    else:
        cred = credentials.Certificate("firebase_credentials.json")

    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("Firebase connected")

except Exception as e:
    print("Firebase ERROR:", e)
    db = None


# =========================
# HOME
# =========================

@app.route("/")
def home():
    return render_template("index.html")


# =========================
# UPLOAD VIDEO
# =========================

@app.route("/upload", methods=["POST"])
def upload():

    try:

        video = request.files["video"]
        title = request.form.get("title", "No title")

        filename = secure_filename(video.filename)

        path = os.path.join(
            UPLOAD_FOLDER,
            filename
        )

        video.save(path)


        print("==============================")
        print("VIDEO UPLOAD STARTED")
        print("File:", filename)
        print("Title:", title)
        print("==============================")


        # SEND TO TELEGRAM

        url = (
            f"https://api.telegram.org/"
            f"bot{TOKEN}/sendVideo"
        )


        with open(path, "rb") as f:

            response = requests.post(
                url,
                data={
                    "chat_id": CHANNEL_ID,
                    "caption": f"Title: {title}"
                },
                files={
                    "video": f
                },
                timeout=120
            )


        data = response.json()


        print("Telegram:", data)


        if not data.get("ok"):
            return jsonify({
                "error": "Telegram upload failed",
                "detail": data
            }),500


        file_id = data["result"]["video"]["file_id"]


        print("Telegram upload SUCCESS")


        # SAVE FIREBASE

        if db:

            db.collection("videos").add({

                "title": title,
                "file_id": file_id,
                "filename": filename

            })

            print("Firebase save SUCCESS")


        return jsonify({

            "success": True,
            "file_id": file_id

        })


    except Exception as e:

        print("SERVER ERROR:", e)

        return jsonify({

            "error": str(e)

        }),500



if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT",5000))
    )