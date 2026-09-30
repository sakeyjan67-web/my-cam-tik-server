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


BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")



# ==========================
# FIREBASE CONNECT
# ==========================

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

                    "video": file

                },

                timeout=120

            )


        result = response.json()



        if not result.get("ok"):

            return jsonify({

                "error": result

            }),500



        file_id = (
            result["result"]
            ["video"]
            ["file_id"]
        )



        # ======================
        # SAVE FIREBASE
        # ======================

        if db:


            doc_ref = db.collection(
                "videos"
            ).document()



            doc_ref.set({

                "video_id":
                doc_ref.id,


                "title":
                title,


                "file_id":
                file_id,


                "filename":
                filename,


                "created_at":
                firestore.SERVER_TIMESTAMP,



                # AUTO ANALYTICS

                "views":0,

                "likes":0,

                "comments":0,

                "shares":0,

                "watch_time":0,

                "completion_rate":0.0,

                "skip_rate":0.0,

                "score":0.0

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
# FEED ALGORITHM
# ==========================

@app.route("/feed")
def feed():

    try:

        if not db:

            return jsonify({
                "error":"Firebase not connected"
            }),500



        videos = []


        docs = db.collection(
            "videos"
        ).stream()



        for doc in docs:


            data = doc.to_dict()



            # SAFE NUMBER CONVERSION

            views = int(
                data.get("views",0) or 0
            )

            likes = int(
                data.get("likes",0) or 0
            )

            comments = int(
                data.get("comments",0) or 0
            )

            shares = int(
                data.get("shares",0) or 0
            )

            watch_time = int(
                data.get("watch_time",0) or 0
            )


            completion = float(
                data.get("completion_rate",0) or 0
            )


            skip = float(
                data.get("skip_rate",0) or 0
            )



            # ======================
            # RECOMMENDATION SCORE
            # ======================


            like_rate = (
                likes / views
                if views else 0
            )


            comment_rate = (
                comments / views
                if views else 0
            )


            share_rate = (
                shares / views
                if views else 0
            )



            score = (

                like_rate * 30

                +

                comment_rate * 20

                +

                share_rate * 35

                +

                completion * 20

                +

                min(watch_time,100) * 0.10

                -

                skip * 20

            )



            videos.append({

                "video_id":
                doc.id,


                "title":
                data.get("title",""),


                "file_id":
                data.get("file_id",""),


                "filename":
                data.get("filename",""),


                "views":
                views,


                "likes":
                likes,


                "comments":
                comments,


                "shares":
                shares,


                "watch_time":
                watch_time,


                "completion_rate":
                completion,


                "skip_rate":
                skip,


                "score":
                round(score,2)

            })



        videos.sort(

            key=lambda x:x["score"],

            reverse=True

        )



        return jsonify(videos)



    except Exception as e:


        return jsonify({

            "error":str(e)

        }),500





# ==========================
# ADD VIEW
# ==========================

@app.route(
    "/view/<video_id>",
    methods=["POST"]
)
def add_view(video_id):

    try:

        db.collection(
            "videos"
        ).document(
            video_id
        ).update({

            "views":
            firestore.Increment(1)

        })


        return jsonify({

            "success":True

        })


    except Exception as e:

        return jsonify({

            "error":str(e)

        }),500





# ==========================
# LIKE
# ==========================

@app.route(
    "/like/<video_id>",
    methods=["POST"]
)
def add_like(video_id):

    try:

        db.collection(
            "videos"
        ).document(
            video_id
        ).update({

            "likes":
            firestore.Increment(1)

        })


        return jsonify({

            "success":True

        })


    except Exception as e:

        return jsonify({

            "error":str(e)

        }),500





# ==========================
# SHARE
# ==========================

@app.route(
    "/share/<video_id>",
    methods=["POST"]
)
def add_share(video_id):

    try:

        db.collection(
            "videos"
        ).document(
            video_id
        ).update({

            "shares":
            firestore.Increment(1)

        })


        return jsonify({

            "success":True

        })


    except Exception as e:

        return jsonify({

            "error":str(e)

        }),500





# ==========================
# WATCH TIME
# ==========================

@app.route(
    "/watch/<video_id>",
    methods=["POST"]
)
def add_watch(video_id):

    try:

        data = request.json or {}


        seconds = int(
            data.get(
                "seconds",
                0
            )
        )


        db.collection(
            "videos"
        ).document(
            video_id
        ).update({

            "watch_time":
            firestore.Increment(seconds)

        })


        return jsonify({

            "success":True

        })


    except Exception as e:

        return jsonify({

            "error":str(e)

        }),500





# ==========================
# SKIP TRACK
# ==========================

@app.route(
    "/skip/<video_id>",
    methods=["POST"]
)
def add_skip(video_id):

    try:

        db.collection(
            "videos"
        ).document(
            video_id
        ).update({

            "skip_rate":
            firestore.Increment(0.01)

        })


        return jsonify({

            "success":True

        })


    except Exception as e:

        return jsonify({

            "error":str(e)

        }),500





# ==========================
# RUN SERVER
# ==========================

if __name__ == "__main__":

    app.run(

        host="0.0.0.0",

        port=5000

    )