import asyncio
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from document_native import OFFICE_TYPES, native_features
from native_relay import LOCAL_ONLY_HEADER, configured_worker, forward_native_document, remote_features
from office_basic import BASIC_OFFICE_EXTENSIONS
from pdf_tools import JobError

router = APIRouter()
MAX_SIZE = 20_000_000
PDF_ACTIONS = {"jpg", "png", "inspect", "merge", "split", "extract", "delete", "rotate",
               "compress", "unlock", "docx", "xlsx", "pptx", "pdfa"}
MIME_BY_EXT = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".zip": "application/zip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


async def process_document(directory, job):
    system = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    env = {**system, "HOME": directory, "TMPDIR": directory, "TEMP": directory, "TMP": directory}
    worker = await asyncio.create_subprocess_exec(
        sys.executable, "-E", "-s", str(Path(__file__).with_name("document_worker.py").resolve()),
        cwd=directory, env=env, start_new_session=sys.platform.startswith("linux"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(worker.communicate(json.dumps(job).encode()), timeout=38)
    except (TimeoutError, asyncio.CancelledError):
        try:
            if sys.platform.startswith("linux"):
                os.killpg(worker.pid, signal.SIGKILL)
            elif worker.returncode is None:
                worker.kill()
        except ProcessLookupError:
            pass
        await worker.wait()
        raise
    if worker.returncode:
        raise HTTPException(422, "The document could not be processed within resource limits.")
    try:
        return json.loads(stdout)
    except ValueError:
        raise HTTPException(422, "The document could not be processed.") from None


@router.get("/document-capabilities")
async def capabilities():
    status = await asyncio.to_thread(native_features)
    worker = configured_worker() if not (status["office"] and status["pdfa"]) else None
    if worker:
        remote = await remote_features(worker)
        status = {**remote, "office": status["office"] or remote["office"],
                  "pdfa": status["pdfa"] or remote["pdfa"]}
    return {"pdf": sorted(PDF_ACTIONS - {"pdfa"}), "html": True, "office_basic": True, **status}

@router.get("/native-worker-capabilities")
async def native_worker_capabilities():
    return await asyncio.to_thread(native_features)


@router.post("/convert-document")
async def convert_document(
    file: list[UploadFile] = File(...),
    action: str = Form(...),
    options: str = Form("{}"),
    password: str | None = Form(None),
    local_only: str = Header(default='', alias=LOCAL_ONLY_HEADER),
):
    if action not in {*PDF_ACTIONS, "html", "office", "office-basic"}:
        raise HTTPException(400, "Choose a supported document operation.")
    worker = None
    if action in {"office", "pdfa"} and not (await asyncio.to_thread(native_features))[action]:
        worker = None if local_only == "1" else configured_worker()
        if not worker:
            raise HTTPException(503, "This tool needs a configured isolated document worker.")
    if (action == "merge" and not 2 <= len(file) <= 6) or (action != "merge" and len(file) != 1):
        raise HTTPException(400, "Merge requires 2 to 6 PDFs. Other operations accept one file.")
    if password is not None and (action != "unlock" or len(password.encode()) > 512):
        raise HTTPException(400, "Invalid password input.")
    try:
        if len(options) > 2048:
            raise ValueError
        settings = json.loads(options)
        if not isinstance(settings, dict):
            raise ValueError
    except (ValueError, RecursionError):
        raise HTTPException(400, "Invalid document options.") from None

    temp = tempfile.TemporaryDirectory(prefix="document_")
    location = Path(temp.name)
    response_ready = False
    try:
        inputs = []
        size = 0
        extensions = {
            "office": set(OFFICE_TYPES),
            "office-basic": BASIC_OFFICE_EXTENSIONS,
            "html": {"html", "htm"},
        }.get(action, {"pdf"})
        for index, item in enumerate(file):
            extension = Path(item.filename or "").suffix.lstrip(".").lower()
            if extension not in extensions:
                raise HTTPException(415, "The file extension does not match the selected document operation.")
            source = location / f"input-{index}.{extension}"
            inputs.append(str(source))
            with source.open("wb") as stream:
                while chunk := await item.read(256 * 1024):
                    size += len(chunk)
                    if size > MAX_SIZE:
                        raise HTTPException(413, "Maximum combined input size is 20 MB.")
                    stream.write(chunk)
        if worker:
            result = await forward_native_document(worker, inputs[0], action)
        else:
            result = await process_document(temp.name, {
                "inputs": inputs, "action": action, "settings": settings, "password": password,
            })
        if "error" in result:
            raise HTTPException(result.get("status", 422), result["error"])
        if "info" in result:
            return result["info"]

        name = result.get("file", "")
        target = location / name
        if not name or Path(name).name != name or target.suffix not in MIME_BY_EXT or target.is_symlink() or not target.is_file():
            raise HTTPException(422, "The converter did not produce a file.")
        if not 0 < target.stat().st_size <= MAX_SIZE:
            raise HTTPException(413, "Output exceeds 20 MB. Try fewer pages.")
        headers = {"x-input-size": str(size)}
        if "note" in result:
            headers["x-conversion-note"] = result["note"]
        response = FileResponse(target, filename="converted" + target.suffix,
                                media_type=MIME_BY_EXT[target.suffix], headers=headers,
                                background=BackgroundTask(temp.cleanup))
        response_ready = True
        return response
    except JobError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    except TimeoutError:
        raise HTTPException(504, "Document conversion timed out. Try a smaller file or fewer pages.") from None
    finally:
        if not response_ready:
            temp.cleanup()
