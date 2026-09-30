# ==========================
# STRICT RECOMMENDATION ENGINE
# ==========================


def calculate_score(video):

    views = video.get(
        "views",
        0
    )

    likes = video.get(
        "likes",
        0
    )

    comments = video.get(
        "comments",
        0
    )

    shares = video.get(
        "shares",
        0
    )


    completion = video.get(
        "completion_rate",
        0
    )


    skip_rate = video.get(
        "skip_rate",
        0
    )


    watch_time = video.get(
        "watch_time",
        0
    )



    # Engagement

    engagement = 0


    if views > 0:

        engagement = (

            (likes / views) * 0.5

            +

            (comments / views) * 0.3

            +

            (shares / views) * 0.2

        )



    # Strict Score

    score = (

        completion * 45

        +

        engagement * 25

        +

        min(watch_time,100) * 0.20

        -

        skip_rate * 30

    )



    # Quality filter

    if completion < 0.20:

        score *= 0.5



    if skip_rate > 0.70:

        score *= 0.3



    return round(
        score,
        4
    )



# ==========================
# SORT FEED
# ==========================


def rank_videos(videos):


    for video in videos:


        video["score"] = calculate_score(
            video
        )


    videos.sort(

        key=lambda x:x["score"],

        reverse=True

    )


    return videos