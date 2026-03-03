from flask import Flask, request, jsonify
import yt_dlp

app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "yt-dlp-api"})

@app.route("/resolve", methods=["POST"])
def resolve():
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

            if not download_url and info.get("requested_formats"):
                fmt = info["requested_formats"][0]
                download_url = fmt.get("url")
                http_headers = fmt.get("http_headers", http_headers)

            if not download_url:
                return jsonify({"error": "ダウンロードURLを取得できませんでした"}), 400

            title = info.get("title", "video")
            ext = info.get("ext", "mp4")
            filename = f"{title}.{ext}"
            filesize = info.get("filesize") or info.get("filesize_approx")

            return jsonify({
                "status": "ok",
                "url": download_url,
                "title": title,
                "filename": filename,
                "ext": ext,
                "filesize": filesize,
                "headers": http_headers,
            })
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"動画の取得に失敗: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
