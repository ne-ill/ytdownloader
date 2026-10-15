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
# Cookie Detection (File or Environment Variable)
# -------------------------------------------------------------
def get_cookie_file():
    """Detects and returns a valid cookie file path, or None."""
    local_cookie = "cookies.txt"
    if os.path.isfile(local_cookie) and os.path.getsize(local_cookie) > 0:
        return local_cookie

    cookie_env = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if cookie_env:
        render_cookie = "render_cookies.txt"
        with open(render_cookie, "w", encoding="utf-8") as f:
            f.write(cookie_env)
        return render_cookie

    return None


def sanitize_filename(name):
    """Sanitizes file name for all operating systems."""
    return re.sub(r'[\\/*?:"<>|]', "", name)


def parse_seconds(t_str):
    """Parses hh:mm:ss, mm:ss, or integer seconds."""
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


def format_error(e):
    """Provides user-friendly error messages for common yt-dlp issues."""
    msg = str(e)
    if "Sign in to confirm you’re not a bot" in msg or "Sign in to confirm you're not a bot" in msg:
        return (
            "YouTube Bot Check: YouTube requires authentication. "
            "Please export cookies.txt from your browser into the yt folder "
            "(or set YOUTUBE_COOKIES env variable on Render)."
        )
    return msg


def get_base_ydl_opts(extra_opts=None):
    """Generates base yt-dlp configuration with bot bypasses and format solvers."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "remote_components": ["ejs:github"],
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
    }
    if shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}

    cookie_path = get_cookie_file()
    if cookie_path:
        opts["cookiefile"] = cookie_path

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
    return "Error: index.html not found.", 404


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
        return jsonify({"error": format_error(e)}), 500


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

    # Clean old temp files
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
                    # Strip timecodes and tags
                    clean_text = re.sub(r'\d+\n\d\d:\d\d:.*?\n', '', raw)
                    clean_text = re.sub(r'<[^>]+>', '', clean_text)
                    txt_path = os.path.join(DOWNLOAD_FOLDER, f"{title}.txt")
                    with open(txt_path, "w", encoding="utf-8") as tf:
                        tf.write(clean_text.strip())
                    return send_file(txt_path, as_attachment=True, download_name=f"{title}.txt")

                return send_file(target_sub, as_attachment=True, download_name=f"{title}.srt")
        except Exception as e:
            return jsonify({"error": format_error(e)}), 500

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
            "format": f"bestvideo[height<={res_limit}]+bestaudio/best[height<={res_limit}]/bestvideo+bestaudio/best",
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
        return jsonify({"error": format_error(e)}), 500


# -------------------------------------------------------------
# 4. Status Check (API)
# -------------------------------------------------------------
@app.route("/api/status", methods=["GET"])
def server_status():
    cookie_file = get_cookie_file()
    return jsonify({
        "status": "online",
        "cookies_active": cookie_file is not None,
        "cookie_file": cookie_file
    })


# -------------------------------------------------------------
# Server Launcher (Local or Render Port)
# -------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    active_cookie = get_cookie_file()
    print(f"\n=======================================================")
    print(f" NEIL STUDIO is LIVE on port {port}")
    if active_cookie:
        print(f" Cookie Authentication: ACTIVE ({active_cookie})")
    else:
        print(" Cookie Authentication: NONE (Running without cookies. Place cookies.txt to activate)")
    print(f"=======================================================\n")
    app.run(host="0.0.0.0", port=port, debug=False)