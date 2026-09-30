from datetime import datetime



# ==========================
# SAVE WATCH EVENT
# ==========================


def create_watch(db,user_id,video_id):


    ref = db.collection(
        "watch_history"
    ).document()



    ref.set({

        "user_id":
        user_id,


        "video_id":
        video_id,


        "start_time":
        datetime.utcnow(),


        "watch_seconds":
        0,


        "completed":
        False,


        "skipped":
        False

    })


    return ref.id




# ==========================
# UPDATE WATCH
# ==========================


def finish_watch(
        db,
        watch_id,
        seconds,
        duration
):


    completion = 0


    if duration > 0:

        completion = seconds / duration



    skipped = False


    if seconds <= 3:

        skipped=True



    completed=False


    if completion >= 0.80:

        completed=True



    db.collection(
        "watch_history"
    ).document(
        watch_id
    ).update({

        "watch_seconds":
        seconds,


        "completion_rate":
        completion,


        "completed":
        completed,


        "skipped":
        skipped,


        "end_time":
        datetime.utcnow()

    })



    return {

        "completion":
        completion,


        "skipped":
        skipped,


        "completed":
        completed

    }