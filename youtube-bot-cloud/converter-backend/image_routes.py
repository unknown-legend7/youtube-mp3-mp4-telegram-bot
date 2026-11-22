import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from image_worker import FORMATS, MAX_BATCH, MAX_BYTES, OUTPUTS
from multipart_files import ordered_uploads

router = APIRouter()
MIMES = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "pdf": "application/pdf"}


async def process_image(manifest, workdir):
    env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    env.update(TEMP=workdir, TMP=workdir, TMPDIR=workdir)
    worker = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(Path(__file__).with_name("image_worker.py").resolve()), str(manifest),
        cwd=workdir, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(worker.communicate(), 35)
    except (asyncio.CancelledError, TimeoutError):
        if worker.returncode is None:
            try:
                worker.kill()
            except ProcessLookupError:
                pass
        await worker.wait()
        raise
    if worker.returncode:
        raise HTTPException(422, "The image could not be processed within resource limits.")
    try:
        return json.loads(stdout)
    except ValueError:
        raise HTTPException(422, "The image could not be processed.") from None


@router.post("/convert-image")
async def convert_image(
    request: Request,
    file: list[UploadFile] = File(...),
    output_format: str = Form(...),
    options: str = Form("{}"),
):
    ext = "jpg" if output_format.lower() == "jpeg" else output_format.lower()
    file = ordered_uploads(file, await request.form(), MAX_BATCH if ext == "pdf" else 1)
    if ext not in OUTPUTS or not 1 <= len(file) <= (MAX_BATCH if ext == "pdf" else 1):
        raise HTTPException(400, "Choose an image output or send one to six images for PDF.")
    try:
        if len(options) > 2048:
            raise ValueError
        settings = json.loads(options)
        if not isinstance(settings, dict):
            raise ValueError
    except (ValueError, RecursionError):
        raise HTTPException(400, "Invalid conversion options.") from None

    temp = tempfile.TemporaryDirectory(prefix="image_")
    directory = Path(temp.name)
    response_ready = False
    total = 0
    try:
        inputs = []
        for index, item in enumerate(file):
            extension = Path(item.filename or "").suffix.lstrip(".").lower()
            mime = item.content_type or ""
            if extension not in FORMATS or not (mime.startswith("image/") or mime == "application/octet-stream"):
                raise HTTPException(415, "Send a JPG, PNG, WebP, BMP or TIFF image.")
            path = directory / f"input-{index}.{extension}"
            with path.open("wb") as stream:
                while chunk := await item.read(256 * 1024):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise HTTPException(413, "Maximum combined input size is 20 MB.")
                    stream.write(chunk)
            inputs.append((str(path), extension))
        output = directory / f"converted.{ext}"
        manifest = directory / "job.json"
        manifest.write_text(json.dumps({
            "inputs": inputs, "output": str(output), "format": ext, "options": settings,
        }), encoding="utf-8")
        result = await process_image(manifest, temp.name)
        if "error" in result:
            raise HTTPException(422, result["error"])
        size = output.stat().st_size
        if not 0 < size <= MAX_BYTES:
            raise HTTPException(413, "Image output exceeds 20 MB.")
        response = FileResponse(output, filename=output.name, media_type=MIMES[ext],
                                headers={"x-input-size": str(total),
                                         "x-target-met": str(result["target_met"]).lower()},
                                background=BackgroundTask(temp.cleanup))
        response_ready = True
        return response
    except TimeoutError:
        raise HTTPException(504, "Image conversion timed out. Try fewer or smaller images.") from None
    finally:
        if not response_ready:
            temp.cleanup()
