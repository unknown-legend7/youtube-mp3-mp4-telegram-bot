from fastapi import HTTPException
from starlette.datastructures import FormData, UploadFile


def ordered_uploads(primary: list[UploadFile], form: FormData, limit: int) -> list[UploadFile]:
    """Accept numbered parts from Telegram and repeated "file" parts from HTTP clients."""
    allowed = {f"file_{index}" for index in range(2, limit + 1)}
    numbered = {}
    for key, value in form.multi_items():
        if not key.startswith("file_"):
            continue
        if key not in allowed:
            raise HTTPException(400, "Invalid numbered upload field.")
        if key in numbered or not isinstance(value, UploadFile):
            raise HTTPException(400, "Each numbered upload field must contain one file.")
        numbered[key] = value
    if not numbered:
        return primary

    keys = [f"file_{index}" for index in range(2, len(numbered) + 2)]
    if len(primary) != 1 or numbered.keys() != set(keys):
        raise HTTPException(400, "Send one file followed by consecutive numbered upload fields.")
    return primary + [numbered[key] for key in keys]
