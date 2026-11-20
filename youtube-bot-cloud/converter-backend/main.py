import asyncio
import hmac
import json
import math
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.datastructures import Headers
from image_routes import router as image_router

API_KEY = os.getenv("CONVERTER_API_KEY", "")
MAX_SIZE = 20_000_000
MAX_REQUEST = MAX_SIZE + 64_000
MAX_SECONDS = 600
BITRATES = {"64k", "128k", "192k", "256k", "320k"}
RESOLUTIONS = {"360p": (640, 360), "480p": (854, 480),
               "720p": (1280, 720), "1080p": (1920, 1080)}
EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".ogg"}
DEMUXERS = "mov,matroska,webm,mp3,wav,aac,ogg"
app = FastAPI(title="Media Conversion API")


def check_key(key):
    if not API_KEY:
        raise HTTPException(503, "Conversion service is unavailable.")
    if not hmac.compare_digest(key.encode(), API_KEY.encode()):
        raise HTTPException(401, "Invalid API key")


class ConversionLimits:
    def __init__(self, app):
        self.app = app
        self.active = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in {"/convert", "/convert-image"}:
            await self.app(scope, receive, send)
            return
        try:
            headers = Headers(scope=scope)
            check_key(headers.get("x-api-key", ""))
            size = headers.get("content-length")
            if size is not None and (not size.isdecimal() or int(size) > MAX_REQUEST):
                raise HTTPException(413, "Maximum input size is 20 MB.")
            if self.active:
                raise HTTPException(429, "Converter is busy. Try again shortly.")
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, exc.status_code)(scope, receive, send)
            return

        self.active = True
        received = 0
        deadline = asyncio.get_running_loop().time() + 45

        async def limited_receive():
            nonlocal received
            try:
                async with asyncio.timeout_at(deadline):
                    message = await receive()
            except TimeoutError:
                raise HTTPException(408, "Upload timed out.") from None
            received += len(message.get("body", b""))
            if received > MAX_REQUEST:
                raise HTTPException(413, "Maximum input size is 20 MB.")
            return message

        try:
            await self.app(scope, limited_receive, send)
        finally:
            self.active = False


app.add_middleware(ConversionLimits)
app.include_router(image_router)


@app.get("/")
async def home():
    return {"status": "online", "service": "Media Conversion API",
            "supported_formats": ["mp3", "mp4"]}


@app.get("/health")
async def health():
    return {"status": "healthy"}


async def run_media_tool(command, timeout, capture=False):
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
        raise
    if process.returncode:
        raise HTTPException(422, "This media could not be read or converted.")
    return output


async def probe(path):
    data = json.loads(await run_media_tool([
        "ffprobe", "-v", "error", "-protocol_whitelist", "file",
        "-format_whitelist", DEMUXERS, "-max_alloc", "67108864",
        "-show_entries", "format=duration:stream=codec_type,width,height",
        "-of", "json", str(path),
    ], 12, capture=True))
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(422, "Could not determine media duration.") from None
    if not math.isfinite(duration) or not 0 < duration <= MAX_SECONDS:
        raise HTTPException(422, "Media must be 10 minutes or shorter.")
    streams = data.get("streams", [])
    if any(s.get("width", 0) * s.get("height", 0) > 4096 * 2160 for s in streams):
        raise HTTPException(422, "Source dimensions exceed the supported limit.")
    return {s["codec_type"] for s in streams if "codec_type" in s}


@app.post("/convert")
async def convert_media(
    file: UploadFile = File(...),
    output_format: str = Form(...),
    audio_bitrate: str = Form("192k"),
    video_resolution: str = Form("720p"),
):
    output_format = output_format.lower()
    extension = Path(file.filename or "").suffix.lower()
    mime = file.content_type or ""
    if extension not in EXTENSIONS or not (
        mime.startswith(("audio/", "video/")) or mime in {"application/octet-stream", "application/ogg"}
    ):
        raise HTTPException(415, "Upload a supported video or audio file.")
    if output_format not in {"mp3", "mp4"} or audio_bitrate not in BITRATES:
        raise HTTPException(400, "Invalid output format or audio bitrate.")
    if video_resolution not in {*RESOLUTIONS, "source"}:
        raise HTTPException(400, "Invalid video resolution.")

    temp = tempfile.TemporaryDirectory(prefix="converter_")
    source = Path(temp.name) / ("input" + extension)
    output = Path(temp.name) / f"converted.{output_format}"
    response_ready = False
    try:
        size = 0
        with source.open("wb") as destination:
            while chunk := await file.read(256 * 1024):
                size += len(chunk)
                if size > MAX_SIZE:
                    raise HTTPException(413, "Maximum input size is 20 MB.")
                destination.write(chunk)

        streams = await probe(source)
        command = [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file",
            "-format_whitelist", DEMUXERS, "-max_alloc", "67108864",
            "-threads", "2", "-i", str(source), "-filter_threads", "1", "-t", str(MAX_SECONDS),
            "-map_metadata", "-1", "-map_chapters", "-1",
        ]
        if output_format == "mp3":
            if "audio" not in streams:
                raise HTTPException(422, "This video has no audio track.")
            command += ["-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", audio_bitrate]
        else:
            if "video" not in streams:
                raise HTTPException(422, "This file has no video track.")
            # Fit the selected box in either orientation, without upscaling.
            width, height = RESOLUTIONS.get(video_resolution, (1920, 1080))
            scale = (f"scale=w='min(iw,if(gte(iw,ih),{width},{height}))':"
                     f"h='min(ih,if(gte(iw,ih),{height},{width}))':"
                     "force_original_aspect_ratio=decrease:force_divisible_by=2")
            command += [
                "-map", "0:V:0", "-map", "0:a:0?", "-vf", scale,
                "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast",
                "-crf", "24", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-b:a", "128k", "-movflags", "+faststart",
            ]
        await run_media_tool(command + ["-fs", str(MAX_SIZE), str(output)], 90)
        if output.stat().st_size >= MAX_SIZE:
            raise HTTPException(413, "Output exceeds 20 MB. Try a shorter clip or lower quality.")
        response = FileResponse(
            output, media_type="audio/mpeg" if output_format == "mp3" else "video/mp4",
            filename=output.name, background=BackgroundTask(temp.cleanup),
        )
        response_ready = True
        return response
    except TimeoutError:
        raise HTTPException(504, "Conversion timed out. Try a shorter clip.") from None
    finally:
        if not response_ready:
            temp.cleanup()


@app.post("/convert-url")
async def convert_url(x_api_key: str = Header(default="")):
    check_key(x_api_key)
    raise HTTPException(503, "YouTube retrieval is currently unavailable. Upload an authorized file.")
