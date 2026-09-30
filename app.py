import os
import json
import requests

import firebase_admin
from firebase_admin import credentials, firestore

from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename


app = Flask(__name__)


# =====================
# SETTINGS
# =====================

UPLOAD_FOLDER = "uploads"

os.makedirs(
    UPLOAD_FOLDER,
    exist_ok=True
)


TOKEN = os.getenv("BOT_TOKEN")

CHANNEL_ID = os.getenv("CHANNEL_ID")



# =====================
# FIREBASE
# =====================

db = None


try:

    firebase_data = os.getenv(
        "FIREBASE_CREDENTIALS"
    )


    if firebase_data:

        cred = credentials.Certificate(
            json.loads(firebase_data)
        )

    else:

        cred = credentials.Certificate(
            "firebase_credentials.json"
        )


    firebase_admin.initialize_app(
        cred
    )


    db = firestore.client()


    print("Firebase Connected")


except Exception as e:

    print(
        "Firebase Error:",
        e
    )



# =====================
# HOME
# =====================

@app.route("/")
def home():

    return render_template(
        "index.html"
    )



# =====================
# VIDEO UPLOAD
# =====================


@app.route(
    "/upload",
    methods=["POST"]
)
def upload():


    try:


        video = request.files["video"]

        title = request.form.get(
            "title",
            "No title"
        )


        filename = secure_filename(
            video.filename
        )


        path = os.path.join(
            UPLOAD_FOLDER,
            filename
        )


        video.save(path)



        # TELEGRAM


        url = (
            "https://api.telegram.org/"
            f"bot{TOKEN}/sendVideo"
        )


        with open(path,"rb") as f:


            r = requests.post(

                url,

                data={

                    "chat_id":CHANNEL_ID,

                    "caption":
                    f"Title: {title}"

                },

                files={

                    "video":f

                },

                timeout=120

            )


        result = r.json()



        if not result.get("ok"):


            return jsonify({

                "error":
                "Telegram failed",

                "detail":
                result

            }),500



        file_id = (
            result["result"]
            ["video"]
            ["file_id"]
        )



        # FIREBASE SAVE


        if db:


            db.collection(
                "videos"
            ).add({

                "title":title,

                "file_id":file_id,

                "filename":filename

            })



        return jsonify({

            "success":True,

            "file_id":file_id

        })



    except Exception as e:


        return jsonify({

            "error":str(e)

        }),500


