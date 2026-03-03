from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import subprocess
import yt_dlp
import tempfile
import os
import uuid
import threading
import time

app = Flask(__name__)
CORS(app)

# Cleanup old temp files periodically
TEMP_DIR = tempfile.mkdtemp(prefix="ytdlp_")

def cleanup_old_files():
    """Remove temp files older than 5 minutes"""
    while True:
        time.sleep(60)
        try:
            now = time.time()
            for f in os.listdir(TEMP_DIR):
                fp = os.path.join(TEMP_DIR, f)
                if os.path.isfile(fp) and now - os.path.getmtime(fp) > 300:
                    os.remove(fp)
        except Exception:
            pass

cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "yt-dlp-api"})


@app.route("/resolve", methods=["POST"])
def resolve():
    """Resolve a URL to get video info and direct download URL"""
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    quality = str(data.get("quality", "1080"))

    if not url:
        return jsonify({"error": "url is required"}), 400

    ydl_opts = {
        "format": f"best[height<={quality}]/bestvideo[height<={quality}]+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            download_url = info.get("url")
            http_headers = info.get("http_headers", {})
            protocol = info.get("protocol", "unknown")

            if not download_url and info.get("requested_formats"):
                fmt = info["requested_formats"][0]
                download_url = fmt.get("url")
                http_headers = fmt.get("http_headers", http_headers)
                protocol = fmt.get("protocol", protocol)

            if not download_url:
                return jsonify({"error": "ダウンロードURLを取得できませんでした"}), 400

            is_hls = protocol in ("m3u8", "m3u8_native") or ".m3u8" in (download_url or "")

            title = info.get("title", "video")
            ext = info.get("ext", "mp4")
            filename = f"{title}.{ext}"
            filesize = info.get("filesize") or info.get("filesize_approx")
            height = info.get("height")
            width = info.get("width")

            return jsonify({
                "status": "ok",
                "url": download_url,
                "title": title,
                "filename": filename,
                "ext": ext,
                "filesize": filesize,
                "headers": http_headers,
                "protocol": protocol,
                "is_hls": is_hls,
                "width": width,
                "height": height,
            })
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"動画の取得に失敗: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/download", methods=["POST"])
def download_video():
    """Download video via yt-dlp (handles HLS, DASH, etc.) and stream the file"""
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    quality = str(data.get("quality", "1080"))

    if not url:
        return jsonify({"error": "url is required"}), 400

    cmd = [
        "python", "-m", "yt_dlp",
        "--format", f"best[height<={quality}]/best",
        "--quiet",
        "--no-warnings",
        "--noplaylist",
        "-o", "-",
        url
    ]

    def generate():
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
            process.terminate()
            process.wait()

    return Response(generate(), mimetype="video/mp4", headers={
        "Content-Disposition": "attachment; filename=\"video.mp4\""
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
