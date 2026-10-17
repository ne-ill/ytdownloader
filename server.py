import os
import re
import glob
import shutil
import yt_dlp
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# -------------------------------------------------------------
# Cookie Detection & Read-Only Workaround for Render
# -------------------------------------------------------------
POSSIBLE_COOKIE_PATHS = [
    "/etc/secrets/cookies.txt",                      # Render Secret File path
    "cookies.txt",                                   # Local folder path
    os.path.join(os.getcwd(), "cookies.txt"),
    os.path.join(os.getcwd(), "downloads", "cookies.txt"),
]

COOKIE_PATH = None
for path in POSSIBLE_COOKIE_PATHS:
    if os.path.exists(path) and os.path.getsize(path) > 0:
        # Render mounts /etc/secrets as read-only.
        # Copy to /tmp/cookies.txt so yt-dlp has write permissions.
        writable_path = "/tmp/cookies.txt" if os.name != "nt" else os.path.join(DOWNLOAD_FOLDER, "active_cookies.txt")
        try:
            shutil.copyfile(path, writable_path)
            COOKIE_PATH = writable_path
        except Exception as e:
            print(f"[NEIL STUDIO] Cookie copy fallback: {e}")
            COOKIE_PATH = path
        break

# Fallback: Environment variable string if set in Render
if not COOKIE_PATH and "YOUTUBE_COOKIES" in os.environ:
    COOKIE_PATH = "/tmp/render_cookies.txt"
    with open(COOKIE_PATH, "w", encoding="utf-8") as f:
        f.write(os.environ["YOUTUBE_COOKIES"])

print(f"\n[NEIL STUDIO AUTH] Cookie status: {'LOADED (Writable copy at ' + COOKIE_PATH + ')' if COOKIE_PATH else 'NOT FOUND'}\n")


def sanitize_filename(name):
    """Sanitizes file names across OS boundaries."""
    return re.sub(r'[\\/*?:"<>|]', "", name)


def parse_seconds(t_str):
    """Parses hh:mm:ss, mm:ss, or numeric seconds."""
    if not t_str:
        return None
    parts = str(t_str).strip().split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None
    return None


def get_base_ydl_opts(extra_opts=None):
    """Generates base yt-dlp config with iOS client bot bypass."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android", "mweb"]
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
            "Accept-Language": "en-US,en;q=0.9",
        }
    }

    if COOKIE_PATH and os.path.exists(COOKIE_PATH):
        opts["cookiefile"] = COOKIE_PATH

    if extra_opts:
        opts.update(extra_opts)
    return opts


# -------------------------------------------------------------
# 1. Homepage Route
# -------------------------------------------------------------
@app.route("/")
def home():
    if os.path.exists("index.html"):
        return send_file("index.html")
    return "Error: index.html not found in project root.", 404


# -------------------------------------------------------------
# 2. Inspect Video Details (API)
# -------------------------------------------------------------
@app.route("/api/info", methods=["POST"])
def get_video_info():
    data = request.get_json() or {}
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    ydl_opts = get_base_ydl_opts({"skip_download": True})

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            duration_secs = info.get("duration", 0)
            mins = duration_secs // 60
            secs = duration_secs % 60
            formatted_duration = f"{mins}:{secs:02d}"

            thumbnail = info.get("thumbnail", "")
            if info.get("thumbnails"):
                thumbnail = info["thumbnails"][-1].get("url", thumbnail)

            return jsonify({
                "id": info.get("id", "Unknown"),
                "title": info.get("title", "YouTube Video"),
                "thumbnail": thumbnail,
                "duration": formatted_duration,
                "duration_seconds": duration_secs,
                "channel": info.get("uploader", "Unknown Channel"),
                "views": f"{info.get('view_count', 0):,}"
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -------------------------------------------------------------
# 3. Download & Export Media (API)
# -------------------------------------------------------------
@app.route("/api/download", methods=["POST"])
def download_media():
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    mode = data.get("mode", "video")         # 'video', 'audio', 'subtitle'
    format_choice = data.get("format", "1080p")
    start_time = data.get("start_time")
    end_time = data.get("end_time")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # Clean old temporary files
    for f in os.listdir(DOWNLOAD_FOLDER):
        fp = os.path.join(DOWNLOAD_FOLDER, f)
        try:
            if os.path.isfile(fp):
                os.remove(fp)
        except Exception:
            pass

    out_template = os.path.join(DOWNLOAD_FOLDER, "%(title)s.%(ext)s")
    ydl_opts = get_base_ydl_opts({"outtmpl": out_template})

    # Optional Time Range Trimmer
    s_sec = parse_seconds(start_time)
    e_sec = parse_seconds(end_time)
    if s_sec is not None and e_sec is not None and e_sec > s_sec:
        ydl_opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(s_sec, e_sec)])
        ydl_opts["force_keyframes_at_cuts"] = True

    # 1. Mode: Subtitles / Transcripts
    if mode == "subtitle":
        sub_type = format_choice.lower()
        ydl_opts.update({
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en", ".*"],
            "subtitlesformat": "srt"
        })
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = sanitize_filename(info.get("title", "transcript"))

                sub_files = glob.glob(os.path.join(DOWNLOAD_FOLDER, "*.srt")) + glob.glob(os.path.join(DOWNLOAD_FOLDER, "*.vtt"))
                if not sub_files:
                    return jsonify({"error": "No subtitles found for this video."}), 404

                target_sub = sub_files[0]

                if sub_type == "txt":
                    with open(target_sub, "r", encoding="utf-8", errors="ignore") as sf:
                        raw = sf.read()
                    clean_text = re.sub(r'\d+\n\d\d:\d\d:.*?\n', '', raw)
                    clean_text = re.sub(r'<[^>]+>', '', clean_text)
                    txt_path = os.path.join(DOWNLOAD_FOLDER, f"{title}.txt")
                    with open(txt_path, "w", encoding="utf-8") as tf:
                        tf.write(clean_text.strip())
                    return send_file(txt_path, as_attachment=True, download_name=f"{title}.txt")

                return send_file(target_sub, as_attachment=True, download_name=f"{title}.srt")
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # 2. Mode: Studio Audio
    elif mode == "audio":
        codec = format_choice.lower()
        postproc = {
            "key": "FFmpegExtractAudio",
            "preferredcodec": codec if codec != "m4a" else "m4a",
        }
        if codec == "mp3":
            postproc["preferredquality"] = "320"

        ydl_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [postproc],
        })

    # 3. Mode: Video
    else:
        res_map = {
            "4k": "2160",
            "1080p": "1080",
            "720p": "720",
            "480p": "480"
        }
        res_limit = res_map.get(format_choice.lower(), "1080")
        ydl_opts.update({
            "format": f"bestvideo[height<={res_limit}]+bestaudio/best[height<={res_limit}]/best",
            "merge_output_format": "mp4"
        })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

            if mode == "audio":
                ext = format_choice.lower()
                filename = os.path.splitext(filename)[0] + f".{ext}"
            else:
                filename = os.path.splitext(filename)[0] + ".mp4"

            clean_title = sanitize_filename(info.get("title", "media"))
            ext = os.path.splitext(filename)[1].replace(".", "")
            download_name = f"{clean_title}.{ext}"

            return send_file(
                filename,
                as_attachment=True,
                download_name=download_name
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# -------------------------------------------------------------
# Server Launcher (Local or Render Port)
# -------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n=======================================================")
    print(f" NEIL STUDIO is LIVE on port {port}")
    if COOKIE_PATH:
        print(f" Cookie Authentication: ACTIVE ({COOKIE_PATH})")
    else:
        print(" Cookie Authentication: NONE (Running in guest mode)")
    print(f"=======================================================\n")
    app.run(host="0.0.0.0", port=port, debug=False)