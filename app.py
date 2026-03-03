from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
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
        "socket_timeout": 30,
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
    """Download video via yt-dlp (handles HLS, DASH, etc.) and return the file"""
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    quality = str(data.get("quality", "1080"))

    if not url:
        return jsonify({"error": "url is required"}), 400

    file_id = str(uuid.uuid4())[:8]
    output_path = os.path.join(TEMP_DIR, f"{file_id}.%(ext)s")
    final_path = None

    ydl_opts = {
        "format": f"best[height<={quality}]/bestvideo[height<={quality}]+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": output_path,
        "merge_output_format": "mp4",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            title = info.get("title", "video")
            ext = info.get("ext", "mp4")

            # Find the downloaded file
            expected = os.path.join(TEMP_DIR, f"{file_id}.{ext}")
            expected_mp4 = os.path.join(TEMP_DIR, f"{file_id}.mp4")

            if os.path.exists(expected_mp4):
                final_path = expected_mp4
            elif os.path.exists(expected):
                final_path = expected
            else:
                # Search for any file with our ID
                for f in os.listdir(TEMP_DIR):
                    if f.startswith(file_id):
                        final_path = os.path.join(TEMP_DIR, f)
                        break

            if not final_path or not os.path.exists(final_path):
                return jsonify({"error": "ダウンロードしたファイルが見つかりません"}), 500

            file_size = os.path.getsize(final_path)
            if file_size == 0:
                return jsonify({"error": "ダウンロードしたファイルが空です"}), 500

            print(f"[download] Sending file: {final_path} ({file_size} bytes) - {title}")

            response = send_file(
                final_path,
                mimetype="video/mp4",
                as_attachment=True,
                download_name=f"{title}.mp4",
            )
            response.headers["X-Video-Title"] = title
            response.headers["X-File-Size"] = str(file_size)

            # Schedule cleanup after response
            @response.call_on_close
            def cleanup():
                try:
                    if final_path and os.path.exists(final_path):
                        os.remove(final_path)
                except Exception:
                    pass

            return response

    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"動画の取得に失敗: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
