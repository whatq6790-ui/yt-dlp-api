from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import yt_dlp
import tempfile
import os
import uuid
import threading
import time
import subprocess
import sys

app = Flask(__name__)
CORS(app)

# Cleanup old temp files periodically
TEMP_DIR = tempfile.mkdtemp(prefix="ytdlp_")

jobs = {}
job_lock = threading.Lock()

def auto_update_ytdlp():
    """Update yt-dlp to latest version in the background on startup"""
    try:
        print("[startup] Updating yt-dlp to latest version...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "yt-dlp"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            print("[startup] yt-dlp updated successfully")
        else:
            print(f"[startup] yt-dlp update failed: {result.stderr}")
    except Exception as e:
        print(f"[startup] yt-dlp auto-update error: {e}")

# Start yt-dlp auto-update in background
threading.Thread(target=auto_update_ytdlp, daemon=True).start()

def cleanup_old_files():
    """Remove temp files older than 10 minutes and old job entries"""
    while True:
        time.sleep(60)
        now = time.time()
        try:
            for f in os.listdir(TEMP_DIR):
                fp = os.path.join(TEMP_DIR, f)
                if os.path.isfile(fp) and now - os.path.getmtime(fp) > 600:
                    try:
                        os.remove(fp)
                    except:
                        pass
        except Exception:
            pass
        
        # Clean jobs older than 10 minutes
        try:
            with job_lock:
                to_delete = []
                for j_id, j_data in jobs.items():
                    if now - j_data.get("created_at", now) > 600:
                        to_delete.append(j_id)
                for j_id in to_delete:
                    del jobs[j_id]
        except Exception:
            pass

cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()


@app.route("/", methods=["GET"])
def health():
    try:
        ytdlp_version = yt_dlp.version.__version__
    except Exception:
        ytdlp_version = "unknown"
    return jsonify({"status": "ok", "service": "yt-dlp-api", "yt_dlp_version": ytdlp_version})


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
def start_download():
    """Start video download async and return a job_id"""
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    quality = str(data.get("quality", "1080"))

    if not url:
        return jsonify({"error": "url is required"}), 400

    job_id = str(uuid.uuid4())[:8]
    with job_lock:
        jobs[job_id] = {
            "status": "downloading", 
            "file": None, 
            "title": None, 
            "size": 0, 
            "error": None,
            "created_at": time.time()
        }

    def bg_download(j_id, d_url, d_qual):
        output_path = os.path.join(TEMP_DIR, f"{j_id}.%(ext)s")

        # Use aria2c for parallel segment downloads (significantly faster for HLS)
        # Falls back to yt-dlp's built-in downloader if aria2c is not available
        aria2c_available = False
        try:
            import shutil
            aria2c_available = shutil.which("aria2c") is not None
        except Exception:
            pass

        ydl_opts = {
            "format": f"best[height<={d_qual}]/bestvideo[height<={d_qual}]+bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "outtmpl": output_path,
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 16,  # parallel HLS segment downloads
        }

        if aria2c_available:
            ydl_opts["external_downloader"] = "aria2c"
            ydl_opts["external_downloader_args"] = {
                "aria2c": [
                    "--max-connection-per-server=16",
                    "--split=16",
                    "--min-split-size=1M",
                    "--max-concurrent-downloads=16",
                    "--file-allocation=none",
                    "--quiet=true",
                ]
            }
            print(f"[download] Using aria2c for parallel downloads: {j_id}")
        else:
            print(f"[download] aria2c not found, using yt-dlp built-in downloader: {j_id}")

        try:
            print(f"[download] Background download started: {j_id}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(d_url, download=True)
                title = info.get("title", "video")
                ext = info.get("ext", "mp4")

                final_path = None
                for candidate in [
                    os.path.join(TEMP_DIR, f"{j_id}.mp4"),
                    os.path.join(TEMP_DIR, f"{j_id}.{ext}"),
                ]:
                    if os.path.exists(candidate):
                        final_path = candidate
                        break
                
                if not final_path:
                    for f in os.listdir(TEMP_DIR):
                        if f.startswith(j_id):
                            final_path = os.path.join(TEMP_DIR, f)
                            break
                            
                with job_lock:
                    if final_path and os.path.exists(final_path):
                        fsize = os.path.getsize(final_path)
                        if fsize > 0:
                            jobs[j_id]["file"] = final_path
                            jobs[j_id]["title"] = title
                            jobs[j_id]["size"] = fsize
                            jobs[j_id]["status"] = "completed"
                            print(f"[download] Background download finished: {j_id} - {fsize} bytes")
                        else:
                            jobs[j_id]["status"] = "error"
                            jobs[j_id]["error"] = "Downloaded file is empty (0 bytes)"
                    else:
                        jobs[j_id]["status"] = "error"
                        jobs[j_id]["error"] = "File not found after download"
        except Exception as e:
            print(f"[download] Background download error: {j_id}: {str(e)}")
            with job_lock:
                jobs[j_id]["status"] = "error"
                jobs[j_id]["error"] = str(e)

    threading.Thread(target=bg_download, args=(job_id, url, quality)).start()
    return jsonify({"job_id": job_id, "status": "downloading"})


@app.route("/status/<job_id>", methods=["GET"])
def check_status(job_id):
    """Check the status of a download job"""
    with job_lock:
        job = jobs.get(job_id)
    
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    return jsonify({
        "status": job["status"],
        "error": job["error"],
        "title": job["title"],
        "size": job["size"]
    })


import urllib.parse

@app.route("/file/<job_id>", methods=["GET"])
def get_file(job_id):
    """Download the completed file via safe streaming"""
    with job_lock:
        job = jobs.get(job_id)
        
    if not job:
        return jsonify({"error": "Job not found"}), 404
        
    if job["status"] != "completed" or not job["file"]:
        return jsonify({"error": "File not ready"}), 400
        
    filepath = job["file"]
    if not os.path.exists(filepath):
        return jsonify({"error": "File was deleted"}), 404

    file_size = os.path.getsize(filepath)
    title = job.get("title", "video")
    safe_title = urllib.parse.quote(title)

    def stream_file():
        try:
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk
        except Exception as e:
            print(f"Stream error: {e}")

    return Response(
        stream_file(),
        mimetype="video/mp4",
        headers={
            "Content-Disposition": 'attachment; filename="video.mp4"',
            "Content-Length": str(file_size),
            "X-Video-Title": safe_title,
            "X-File-Size": str(file_size),
            "Access-Control-Expose-Headers": "X-Video-Title, X-File-Size, Content-Disposition"
        }
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
