from contextlib import contextmanager
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

from PIL import Image
from pypdf import PdfReader, PdfWriter

MAX_PAGES = 40
MAX_RENDER = 20
MAX_OUTPUT_BYTES = 20_000_000
EXCLUDED = ["/Annots", "/AA", "/B", "/PresSteps", "/Trans", "/Dur"]


class JobError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def whole(value, start, stop, label):
    if type(value) is not int or not start <= value <= stop:
        raise JobError(f"{label} must be from {start} to {stop}.")
    return value


def pdf_reader(path, password=None):
    with Path(path).open("rb") as file:
        if b"%PDF-" not in file.read(1024):
            raise JobError("Upload a valid PDF file.", 415)
    reader = PdfReader(path)
    encrypted = reader.is_encrypted
    if encrypted:
        if password is None:
            raise JobError("This PDF is encrypted. Use Unlock PDF with the existing password.", 423)
        if not password or not reader.decrypt(password):
            raise JobError("The PDF password is incorrect.", 403)
    if not 1 <= len(reader.pages) <= MAX_PAGES:
        raise JobError(f"PDFs must contain 1 to {MAX_PAGES} pages.")
    return reader, encrypted


def page_indices(selection, length):
    if selection == "all":
        return list(range(length))
    if selection == "first":
        return [0]
    if not isinstance(selection, str) or not selection or len(selection) > 180:
        raise JobError("Use first, all, or page numbers such as 1-3,5.")
    pages = []
    for term in selection.split(","):
        bounds = term.strip().split("-")
        if len(bounds) > 2 or any(not part.isascii() or not part.isdecimal() for part in bounds):
            raise JobError("Use first, all, or page numbers such as 1-3,5.")
        start, stop = int(bounds[0]), int(bounds[-1])
        if not 1 <= start <= stop <= length:
            raise JobError("The selected pages are outside the document.")
        pages.extend(range(start - 1, stop))
    return list(dict.fromkeys(pages))


@contextmanager
def rendered_pages(path, pages, dpi):
    import pypdfium2 as pdfium

    if not 1 <= len(pages) <= MAX_RENDER:
        raise JobError(f"Select up to {MAX_RENDER} pages per rendered conversion.")
    scale = whole(dpi, 48, 200, "DPI") / 72
    document = pdfium.PdfDocument(path)

    def pictures():
        for index in pages:
            page = document[index]
            try:
                width, height = page.get_size()
                if not 0 < width * scale <= 6000 or not 0 < height * scale <= 6000 or width * height * scale**2 > 8_000_000:
                    raise JobError("A page is too large at that DPI. Choose a lower DPI.")
                bitmap = page.render(scale=scale, draw_annots=False)
                try:
                    yield index, bitmap.to_pil().copy()
                finally:
                    bitmap.close()
            finally:
                page.close()

    iterator = pictures()
    try:
        yield iterator
    finally:
        iterator.close()
        document.close()


def write_pages(readers, output, indices=None, rotate=0, compression=None):
    with PdfWriter() as writer:
        for reader in readers:
            for index in range(len(reader.pages)):
                if indices is not None and index not in indices:
                    continue
                page = writer.add_page(reader.pages[index], excluded_keys=EXCLUDED)
                if rotate and index in rotate[0]:
                    page.rotate(rotate[1])
        if compression:
            level, bound = {"low": (None, None), "balanced": (82, 1600), "high": (62, 1080)}[compression]
            seen = set()
            for page in writer.pages:
                if level:
                    for image in page.images:
                        ref = image.indirect_reference
                        if ref is None or ref.idnum in seen:
                            continue
                        seen.add(ref.idnum)
                        source = image.image
                        if source is None or source.mode not in {"RGB", "L"} or source.width * source.height > 8_000_000:
                            continue
                        with source.copy() as resized:
                            resized.thumbnail((bound, bound), Image.Resampling.LANCZOS)
                            image.replace(resized, quality=level)
                page.compress_content_streams(level=9)
            writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
        writer.write(output)


def raster_pdf(path, output, pages, dpi, kind):
    suffix = "jpg" if kind == "jpg" else "png"
    with rendered_pages(path, pages, dpi) as pictures:
        if len(pages) == 1:
            _, image = next(pictures)
            with image:
                image.save(output, "JPEG" if suffix == "jpg" else "PNG")
        else:
            with ZipFile(output, "w", compression=ZIP_STORED) as archive:
                for index, image in pictures:
                    temporary = output.parent / f"page-{index + 1:03}.{suffix}"
                    with image:
                        image.save(temporary, "JPEG" if suffix == "jpg" else "PNG")
                    archive.write(temporary, temporary.name)
                    temporary.unlink()
                    if output.stat().st_size > MAX_OUTPUT_BYTES:
                        raise JobError("Output exceeds 20 MB. Select fewer pages or a lower DPI.", 413)


def pdf_operation(inputs, directory, action, settings, password):
    path = inputs[0]
    if action == "unlock" and not password:
        raise JobError("Enter the existing PDF password.", 400)
    reader, encrypted = pdf_reader(path, password if action == "unlock" else None)
    count = len(reader.pages)
    if action == "unlock" and not encrypted:
        raise JobError("This PDF is already unlocked.")
    if action == "inspect":
        return {"info": {"pages": count, "pdf_version": reader.pdf_header.lstrip("%"),
                         "page_width_pt": round(float(reader.pages[0].mediabox.width)),
                         "page_height_pt": round(float(reader.pages[0].mediabox.height))}}
    if action in {"jpg", "png"}:
        pages = page_indices(settings.get("pages", "first"), count)
        output = directory / ("converted." + (action if len(pages) == 1 else "zip"))
        raster_pdf(path, output, pages, settings.get("dpi", 120), action)
        return {"file": output.name}

    output = directory / "converted.pdf"
    result = {"file": output.name}
    if action == "merge":
        others = [pdf_reader(item)[0] for item in inputs[1:]]
        if sum(len(item.pages) for item in [reader, *others]) > MAX_PAGES:
            raise JobError(f"Merged PDFs may contain up to {MAX_PAGES} pages total.")
        write_pages([reader, *others], output)
    elif action in {"extract", "split", "delete", "rotate"}:
        pages = page_indices(settings.get("pages"), count)
        if action == "split":
            remaining = [index for index in range(count) if index not in pages]
            if not remaining:
                raise JobError("Split needs some pages on each side. Use Extract for one file.")
            first, second = directory / "selected.pdf", directory / "remaining.pdf"
            write_pages([reader], first, pages)
            write_pages([reader], second, remaining)
            archive_path = directory / "split.zip"
            with ZipFile(archive_path, "w", compression=ZIP_STORED) as archive:
                archive.write(first, first.name)
                archive.write(second, second.name)
            return {"file": archive_path.name}
        if action == "delete":
            pages = [index for index in range(count) if index not in pages]
            if not pages:
                raise JobError("At least one page must remain.")
        if action == "rotate":
            angle = settings.get("angle")
            if angle not in (90, 180, 270):
                raise JobError("Choose 90, 180, or 270 degrees.")
            write_pages([reader], output, rotate=(pages, angle))
        else:
            write_pages([reader], output, pages)
    elif action == "compress":
        profile = settings.get("level", "balanced")
        if profile not in ("low", "balanced", "high"):
            raise JobError("Choose low, balanced, or high PDF compression.")
        write_pages([reader], output, compression=profile)
    elif action == "unlock":
        write_pages([reader], output)
        result["note"] = "Password removed. Interactive annotations and forms were omitted."
    else:
        raise JobError("Unsupported PDF operation.", 400)
    return result
