
import os
import uuid
import asyncio
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

app = FastAPI(title="Media Conversion API")

API_KEY = os.getenv("CONVERTER_API_KEY", "")
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB

ALLOWED_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm",
    ".mp3", ".wav", ".m4a", ".aac", ".ogg"
}

def cleanup(path: str):
    shutil.rmtree(path, ignore_errors=True)

def check_api_key(key: str):
    if not API_KEY:
        raise HTTPException(500, "Server API key is not configured")

    if key != API_KEY:
        raise HTTPException(401, "Invalid API key")

@app.get("/")
async def home():
    return {
        "status": "online",
        "service": "Media Conversion API",
        "supported_formats": ["mp3", "mp4"]
    }

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.post("/convert")
async def convert_media(
    file: UploadFile = File(...),
    output_format: str = Form(...),
    audio_bitrate: str = Form("192k"),
    video_resolution: str = Form("720p"),
    x_api_key: str = Header(default="")
):
    check_api_key(x_api_key)

    output_format = output_format.lower()

    if output_format not in ["mp3", "mp4"]:
        raise HTTPException(400, "Format must be mp3 or mp4")

    extension = Path(file.filename or "").suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "Unsupported input file type")

    allowed_bitrates = ["128k", "192k", "256k", "320k"]

    if audio_bitrate not in allowed_bitrates:
        raise HTTPException(400, "Invalid audio bitrate")

    resolutions = {
        "360p": "640x360",
        "480p": "854x480",
        "720p": "1280x720",
        "1080p": "1920x1080"
    }

    if video_resolution not in resolutions:
        raise HTTPException(400, "Invalid resolution")

    temp_dir = tempfile.mkdtemp(prefix="converter_")

    input_path = os.path.join(temp_dir, "input" + extension)
    output_path = os.path.join(temp_dir, f"output.{output_format}")

    try:
        size = 0

        with open(input_path, "wb") as destination:
            while True:
                chunk = await file.read(1024 * 1024)

                if not chunk:
                    break

                size += len(chunk)

                if size > MAX_FILE_SIZE:
                    raise HTTPException(413, "File exceeds 100 MB limit")

                destination.write(chunk)

        if output_format == "mp3":
            command = [
                "ffmpeg",
                "-y",
                "-i", input_path,
                "-vn",
                "-codec:a", "libmp3lame",
                "-b:a", audio_bitrate,
                output_path
            ]
        else:
            resolution = resolutions[video_resolution]

            command = [
                "ffmpeg",
                "-y",
                "-i", input_path,
                "-vf", f"scale={resolution}:force_original_aspect_ratio=decrease",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                output_path
            ]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )

        _, stderr = await process.communicate()

        if process.returncode != 0:
            error = stderr.decode(errors="ignore")[-2000:]
            print("FFmpeg error:", error)
            raise HTTPException(500, "Conversion failed")

        if not os.path.exists(output_path):
            raise HTTPException(500, "Output file was not created")

        return FileResponse(
            output_path,
            media_type=(
                "audio/mpeg" if output_format == "mp3"
                else "video/mp4"
            ),
            filename=f"converted.{output_format}",
            background=BackgroundTask(cleanup, temp_dir)
        )

    except Exception:
        cleanup(temp_dir)
        raise

    finally:
        await file.close()
