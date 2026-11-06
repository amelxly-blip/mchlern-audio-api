import os
import base64
import tempfile
import urllib.parse
import re

import requests
import yt_dlp
import imageio_ffmpeg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, HTMLResponse
from pydantic import BaseModel


app = FastAPI(title="MCHLERN Audio Fetcher API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class FetchRequest(BaseModel):
    url: str
    cookies: str = ""


def resolve_spotify(url: str):
    if "spotify.com" in url:
        try:
            oembed = f"https://open.spotify.com/oembed?url={urllib.parse.quote(url)}"
            r = requests.get(oembed, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                d = r.json()
                title = d.get("title", "").strip()
                thumb = d.get("thumbnail_url", "")
                if title:
                    return f"ytsearch1:{title}", thumb
        except Exception:
            pass
    return url, ""


@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html><body style="font-family:monospace;background:#0a0a0a;color:#fff;padding:40px">
    <h1>🎵 MCHLERN Audio Fetcher API</h1>
    <p>Status: <span style="color:#22c55e">🟢 Online (Ultra-Fast)</span></p>
    <p>Endpoint: <code>POST /api/fetch-audio</code></p>
    </body></html>
    """


@app.get("/health")
async def health():
    return {"status": "ok"}


def clean_error_message(err_str: str) -> str:
    s = str(err_str)
    if "unavailable" in s.lower():
        return "Video tidak tersedia di YouTube (dihapus atau private)."
    if "private video" in s.lower():
        return "Video YouTube ini bersifat Private dan tidak dapat diakses."
    if "bot" in s.lower() or "sign in" in s.lower():
        return "YouTube meminta verifikasi login (bot check). Perbarui cookies di Admin Panel."
    if "georestricted" in s.lower() or "not available in your country" in s.lower():
        return "Video ini dibatasi secara geografis (region-locked) oleh YouTube."
    s = re.sub(r"^ERROR:\s*\[[^\]]+\]\s*", "", s).strip()
    return s[:200] if s else "Gagal mengambil audio dari YouTube."


@app.post("/api/fetch-audio")
async def fetch_audio(req: FetchRequest):
    raw_url = req.url.strip()
    if not raw_url:
        raise HTTPException(status_code=400, detail="URL tidak boleh kosong.")

    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    url, spotify_thumb = resolve_spotify(raw_url)
    temp_dir = tempfile.gettempdir()

    # Siapkan file cookie jika disediakan
    cookie_file = None
    if req.cookies and req.cookies.strip():
        c_text = req.cookies.strip()
        try:
            decoded = base64.b64decode(c_text).decode("utf-8")
            if "\t" in decoded:
                c_text = decoded
        except Exception:
            pass
        cf_path = os.path.join(temp_dir, "yt_cookies.txt")
        try:
            with open(cf_path, "w", encoding="utf-8") as f:
                f.write(c_text)
            cookie_file = cf_path
        except Exception:
            cookie_file = None

    # STRATEGI CEPAT:
    # 1. Coba android TANPA cookies dulu (super cepat ~3 detik, bypass bot check)
    # 2. Coba tv_embedded / web TANPA cookies
    # 3. Baru jika ada cookie_file dan gagal, coba dengan cookies
    attempts = [
        ("android", None),
        ("tv_embedded", None),
        ("web", None),
    ]
    if cookie_file:
        attempts.append(("android", cookie_file))
        attempts.append(("web", cookie_file))

    last_err = None

    for client, c_file in attempts:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "ba[ext=m4a]/bestaudio/b/best",
            "outtmpl": os.path.join(temp_dir, "mchlern_%(id)s.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "ffmpeg_location": ffmpeg_path,
            "extractor_args": {"youtube": {"player_client": [client]}},
            "nocheckcertificate": True,
            "noplaylist": True,
            "socket_timeout": 12,
            "max_filesize": 25 * 1024 * 1024,
        }
        if c_file:
            ydl_opts["cookiefile"] = c_file

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    raise Exception("yt-dlp tidak mengembalikan data.")
                if "entries" in info:
                    info = info["entries"][0]

                title = info.get("title") or "YouTube Audio"
                thumb = spotify_thumb or info.get("thumbnail") or ""
                duration = info.get("duration") or 0

                # Roblox limit 7 menit
                if duration and duration > 420:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Durasi audio ({int(duration // 60)}m {int(duration % 60)}s) melebihi batas Roblox (maks 7 menit)."
                    )

                raw_file = ydl.prepare_filename(info)
                mp3_file = os.path.splitext(raw_file)[0] + ".mp3"

                if not os.path.isfile(mp3_file):
                    raise FileNotFoundError("File MP3 tidak ditemukan.")

                with open(mp3_file, "rb") as f_audio:
                    audio_bytes = f_audio.read()

                try:
                    os.remove(mp3_file)
                except Exception:
                    pass

                clean_title = re.sub(r'[/\\?%*:|"<>#]', '_', title)[:50].strip() or "Audio"
                return Response(
                    content=audio_bytes,
                    media_type="audio/mpeg",
                    headers={
                        "Content-Disposition": f'attachment; filename="{urllib.parse.quote(clean_title)}.mp3"',
                        "X-Title": urllib.parse.quote(clean_title),
                        "X-Thumbnail": urllib.parse.quote(thumb),
                        "X-Duration": str(duration),
                        "Access-Control-Expose-Headers": "X-Title, X-Thumbnail, X-Duration",
                    }
                )
        except HTTPException:
            raise
        except Exception as e:
            last_err = str(e)
            # Jika video memang unavailable/private, jangan buang waktu coba client lain
            if "unavailable" in last_err.lower() or "private video" in last_err.lower():
                break
            continue

    friendly = clean_error_message(last_err or "Semua client gagal")
    raise HTTPException(status_code=502, detail=friendly)
