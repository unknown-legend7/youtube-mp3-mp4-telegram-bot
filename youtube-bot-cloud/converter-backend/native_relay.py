import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from pdf_tools import JobError, MAX_OUTPUT_BYTES

LOCAL_ONLY_HEADER = "x-native-local-only"
ARCHIVE_NOTE = "PDF/A-2b verified by veraPDF."


@dataclass(frozen=True)
class RemoteNativeWorker:
    origin: str
    key: str


def configured_worker():
    origin = os.getenv("NATIVE_WORKER_URL", "").strip().rstrip("/")
    key = os.getenv("NATIVE_WORKER_API_KEY", "").strip()
    try:
        parts = urlsplit(origin)
        valid = (parts.scheme == "https" and parts.hostname and not parts.username
                 and not parts.password and not parts.path and not parts.query
                 and not parts.fragment and not any(char.isspace() for char in origin))
        if not valid or not key or not key.isascii() or any(not 33 <= ord(char) <= 126 for char in key):
            return None
        if parts.port is not None and not 1 <= parts.port <= 65535:
            return None
    except ValueError:
        return None
    return RemoteNativeWorker(origin, key)


def client(timeout):
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)


async def remote_features(worker):
    unavailable = {"office": False, "pdfa": False, "native_status": "remote_unavailable"}
    try:
        async with asyncio.timeout(6), client(5) as session:
            async with session.stream(
                "GET", worker.origin + "/native-worker-capabilities",
                headers={"x-api-key": worker.key, "Accept-Encoding": "identity"},
            ) as response:
                if response.status_code != 200:
                    return unavailable
                data = bytearray()
                async for piece in response.aiter_raw(chunk_size=4096):
                    data.extend(piece)
                    if len(data) > 4096:
                        return unavailable
                status = json.loads(data)
                if not isinstance(status, dict):
                    return unavailable
    except (httpx.HTTPError, TimeoutError, ValueError, RecursionError):
        return unavailable
    features = {action: status.get(action) is True for action in ("office", "pdfa")}
    return {**features, "native_status": "remote_ready" if all(features.values()) else
            "remote_partially_ready" if any(features.values()) else "remote_unavailable"}


async def forward_native_document(worker, source, action):
    path = Path(source)
    output = path.with_name("converted.pdf")
    try:
        async with asyncio.timeout(45), client(42) as session:
            with path.open("rb") as upload:
                async with session.stream(
                    "POST", worker.origin + "/convert-document",
                    headers={"x-api-key": worker.key, LOCAL_ONLY_HEADER: "1", "Accept-Encoding": "identity"},
                    files={"file": (path.name, upload, "application/octet-stream")},
                    data={"action": action},
                ) as response:
                    if response.status_code in (400, 403, 413, 415, 422, 423, 429, 503, 504):
                        return {"status": response.status_code,
                                "error": "The isolated worker could not process this document."}
                    if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != "application/pdf":
                        raise JobError("The isolated worker returned an invalid response.", 502)
                    if action == "pdfa" and response.headers.get("x-conversion-note") != ARCHIVE_NOTE:
                        raise JobError("The isolated worker did not confirm PDF/A-2b validation.", 502)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise JobError("The isolated worker returned an unexpected encoding.", 502)
                    size = 0
                    with output.open("wb") as destination:
                        async for piece in response.aiter_raw(chunk_size=64 * 1024):
                            size += len(piece)
                            if size > MAX_OUTPUT_BYTES:
                                raise JobError("Output exceeds 20 MB. Try fewer pages.", 413)
                            destination.write(piece)
        with output.open("rb") as converted:
            if converted.read(5) != b"%PDF-":
                raise JobError("The isolated worker did not return a PDF.", 502)
    except (httpx.TimeoutException, TimeoutError):
        raise JobError("The isolated document worker timed out.", 504) from None
    except httpx.HTTPError:
        raise JobError("The isolated document worker is unavailable.", 503) from None
    note = ARCHIVE_NOTE if action == "pdfa" else "Fonts, formulas, animations and complex formatting may differ."
    return {"file": output.name, "note": note}
