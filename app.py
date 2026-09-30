import os
import json
import requests
from datetime import datetime

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


BOT_TOKEN = os.getenv("BOT_TOKEN")

CHANNEL_ID = os.getenv("CHANNEL_ID")


# ==========================
# FIREBASE
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

    print("Firebase Connected")


except Exception as e:

    print(
        "Firebase Error:",
        e
    )



# ==========================
# HOME
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
                "error":"No video found"
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


        video.save(filepath)



        telegram_url = (
            "https://api.telegram.org/"
            f"bot{BOT_TOKEN}/sendVideo"
        )


        with open(filepath,"rb") as file:


            response = requests.post(

                telegram_url,

                data={

                    "chat_id": CHANNEL_ID,

                    "caption": title

                },

                files={

                    "video":file

                },

                timeout=120

            )



        result = response.json()



        if not result.get("ok"):


            return jsonify({

                "error":"Telegram failed",

                "detail":result

            }),500



        file_id = (

            result["result"]
            ["video"]
            ["file_id"]

        )



        # SAVE VIDEO DATA

        if db:


            db.collection(
                "videos"
            ).add({

                "title":title,

                "file_id":file_id,

                "filename":filename,

                "views":0,

                "likes":0,

                "comments":0,

                "shares":0,

                "watch_time":0,

                "completion_rate":0,

                "skip_rate":0,

                "created_at":
                datetime.utcnow()

            })



        return jsonify({

            "success":True,

            "file_id":file_id

        })



    except Exception as e:


        return jsonify({

            "error":str(e)

        }),500




# ==========================
# WATCH START
# ==========================

@app.route(
"/video/start",
methods=["POST"]
)
def video_start():

    data = request.json


    ref = db.collection(
        "watch_history"
    ).document()


    ref.set({

        "user_id":
        data["user_id"],

        "video_id":
        data["video_id"],

        "start":
        datetime.utcnow()

    })


    return jsonify({

        "watch_id":ref.id

    })




# ==========================
# WATCH END / SWIPE
# ==========================

@app.route(
"/video/end",
methods=["POST"]
)
def video_end():


    data=request.json


    seconds=data["watch_seconds"]

    duration=data["video_length"]


    completion = 0


    if duration:

        completion = seconds/duration



    skipped=False


    if seconds < 3:

        skipped=True



    db.collection(
        "watch_history"
    ).document(
        data["watch_id"]
    ).update({

        "watch_seconds":
        seconds,

        "completion_rate":
        completion,

        "skipped":
        skipped,

        "end":
        datetime.utcnow()

    })



    return jsonify({

        "completion_rate":
        completion,

        "skipped":
        skipped

    })




# ==========================
# FEED ALGORITHM
# ==========================

@app.route("/feed")
def feed():


    videos=[]


    docs=db.collection(
        "videos"
    ).stream()



    for doc in docs:


        video=doc.to_dict()



        views=video.get(
            "views",
            0
        )

        likes=video.get(
            "likes",
            0
        )

        watch=video.get(
            "completion_rate",
            0
        )


        score=(

            watch*50

            +

            (likes/views if views else 0)*30

            -

            video.get(
                "skip_rate",
                0
            )*20

        )



        video["score"]=score


        videos.append(video)



    videos.sort(

        key=lambda x:x["score"],

        reverse=True

    )


    return jsonify(videos)




# ==========================
# RUN
# ==========================

if __name__=="__main__":


    app.run(

        host="0.0.0.0",

        port=5000

    )