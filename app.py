from flask import Flask, jsonify, render_template, request
import requests
import firebase_admin
from firebase_admin import credentials, firestore

# =========================
# Firebase
# =========================

cred = credentials.Certificate("firebase_credentials.json")
firebase_admin.initialize_app(cred)

db = firestore.client()

# =========================
# Flask
# =========================

app = Flask(__name__)

# =========================
# Telegram
# =========================

TOKEN = "8894560001:AAGwZm-jID0rgDdpD3DX9HZGcubbtTOeqr8"
CHANNEL_ID = "-1004318297083"


# =========================
# Home
# =========================

@app.route("/")
def home():
    return render_template("index.html")


# =========================
# Upload
# =========================

@app.route("/upload", methods=["POST"])
def upload_video():

    try:
        title = request.form.get("title", "No Title")
        video_file = request.files.get("video")

        if not video_file:
            return jsonify({
                "error": "Video file nahi mili!"
            }), 400

        print("\n==============================")
        print("VIDEO UPLOAD STARTED")
        print("File:", video_file.filename)
        print("Title:", title)
        print("==============================")

        # Telegram API
        url = f"https://api.telegram.org/bot{TOKEN}/sendVideo"

        files = {
            "video": (
                video_file.filename,
                video_file.read(),
                video_file.content_type
            )
        }

        data = {
            "chat_id": CHANNEL_ID,
            "caption": f"Title: {title}"
        }

        print("Sending video to Telegram...")

        # Telegram request
        response = requests.post(
            url,
            files=files,
            data=data,
            timeout=120
        )

        print("Telegram HTTP status:", response.status_code)
        print("Telegram raw response:", response.text)

        # JSON response
        try:
            res_json = response.json()
        except Exception:
            return jsonify({
                "error": "Telegram ne valid JSON response nahi diya",
                "http_status": response.status_code,
                "raw_response": response.text
            }), 500

        print("Telegram JSON:", res_json)

        # =========================
        # SUCCESS
        # =========================

        if res_json.get("ok"):

            result = res_json.get("result", {})

            video = result.get("video", {})
            file_id = video.get("file_id")

            print("Telegram upload SUCCESS")
            print("File ID:", file_id)

            # Firebase
            db.collection("videos").add({
                "title": title,
                "file_id": file_id,
                "created_at": firestore.SERVER_TIMESTAMP
            })

            print("Firebase save SUCCESS")
            print("==============================\n")

            return jsonify({
                "success": True,
                "message": "Video uploaded successfully!",
                "file_id": file_id
            }), 200

        # =========================
        # TELEGRAM ERROR
        # =========================

        description = res_json.get(
            "description",
            "Unknown Telegram error"
        )

        error_code = res_json.get(
            "error_code",
            response.status_code
        )

        print("TELEGRAM ERROR")
        print("Error code:", error_code)
        print("Description:", description)
        print("==============================\n")

        return jsonify({
            "error": "Telegram upload failed",
            "error_code": error_code,
            "description": description,
            "telegram_response": res_json
        }), 500

    # =========================
    # TIMEOUT
    # =========================

    except requests.exceptions.Timeout:

        print("ERROR: Telegram request timed out")

        return jsonify({
            "error": "Telegram request timed out",
            "message": "Windscribe VPN check karo aur dobara try karo."
        }), 504

    # =========================
    # NETWORK ERROR
    # =========================

    except requests.exceptions.RequestException as e:

        print("NETWORK ERROR:", str(e))

        return jsonify({
            "error": "Telegram network error",
            "details": str(e)
        }), 500

    # =========================
    # OTHER ERROR
    # =========================

    except Exception as e:

        print("SERVER ERROR:", str(e))

        return jsonify({
            "error": "Server error",
            "details": str(e)
        }), 500


# =========================
# Run
# =========================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )