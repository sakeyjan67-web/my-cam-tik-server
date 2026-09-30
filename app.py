import os
import json
import requests

import firebase_admin
from firebase_admin import credentials, firestore

from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename


# ==========================
# FLASK APP
# ==========================

app = Flask(
    __name__,
    template_folder="templates"
)


# ==========================
# SETTINGS
# ==========================

UPLOAD_FOLDER = "/tmp/uploads"

os.makedirs(
    UPLOAD_FOLDER,
    exist_ok=True
)


BOT_TOKEN = os.getenv(
    "BOT_TOKEN"
)


CHANNEL_ID = os.getenv(
    "CHANNEL_ID"
)



# ==========================
# FIREBASE CONNECT
# ==========================

db = None


try:

    firebase_data = os.getenv(
        "FIREBASE_CREDENTIALS"
    )


    if firebase_data:

        firebase_json = json.loads(
            firebase_data
        )

        cred = credentials.Certificate(
            firebase_json
        )


    else:

        cred = credentials.Certificate(
            "firebase_credentials.json"
        )


    if not firebase_admin._apps:

        firebase_admin.initialize_app(
            cred
        )


    db = firestore.client()


    print(
        "Firebase Connected"
    )


except Exception as e:

    print(
        "Firebase Error:",
        e
    )



# ==========================
# HOME PAGE
# ==========================

@app.route("/")
def home():

    return render_template(
        "index.html"
    )



# ==========================
# UPLOAD VIDEO
# ==========================

@app.route(
    "/upload",
    methods=["POST"]
)
def upload():

    try:


        if "video" not in request.files:

            return jsonify({

                "error":
                "No video found"

            }),400



        video = request.files["video"]


        title = request.form.get(
            "title",
            "No title"
        )



        filename = secure_filename(
            video.filename
        )


        filepath = os.path.join(
            UPLOAD_FOLDER,
            filename
        )


        video.save(
            filepath
        )



        print(
            "Sending to Telegram..."
        )



        telegram_url = (
            "https://api.telegram.org/"
            f"bot{BOT_TOKEN}/sendVideo"
        )



        with open(filepath,"rb") as file:


            response = requests.post(

                telegram_url,

                data={

                    "chat_id":
                    CHANNEL_ID,

                    "caption":
                    f"Title: {title}"

                },


                files={

                    "video":
                    file

                },


                timeout=120

            )



        telegram_result = response.json()



        print(
            telegram_result
        )



        if not telegram_result.get("ok"):


            return jsonify({

                "error":
                "Telegram upload failed",

                "detail":
                telegram_result

            }),500




        file_id = (

            telegram_result
            ["result"]
            ["video"]
            ["file_id"]

        )



        print(
            "Telegram Success"
        )



        # FIREBASE SAVE

        if db:


            db.collection(
                "videos"
            ).add({

                "title":
                title,

                "file_id":
                file_id,

                "filename":
                filename

            })



            print(
                "Firebase Save Success"
            )



        return jsonify({

            "success":
            True,

            "file_id":
            file_id

        })



    except Exception as e:


        print(
            "SERVER ERROR:",
            e
        )


        return jsonify({

            "error":
            str(e)

        }),500




# ==========================
# LOCAL RUN
# ==========================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000
    )