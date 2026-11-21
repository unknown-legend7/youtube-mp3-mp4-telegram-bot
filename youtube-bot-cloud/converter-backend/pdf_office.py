import io
from pathlib import Path

from pdf_tools import JobError, page_indices, pdf_reader, rendered_pages


def table_rows(page):
    for table in page.find_tables()[:20]:
        rows = table.extract()
        if len(rows) >= 2 and any(sum(bool(value and value.strip()) for value in row) >= 2 for row in rows):
            if len(rows) > 500 or any(len(row) > 40 for row in rows):
                raise JobError("An extracted table exceeds 500 rows or 40 columns.")
            yield rows


def pdf_to_xlsx(path, pages, output):
    import pdfplumber
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    with pdfplumber.open(path, unicode_norm="NFC") as pdf:
        for index in pages:
            for number, rows in enumerate(table_rows(pdf.pages[index]), 1):
                if len(workbook.worksheets) >= 40:
                    raise JobError("Too many tables. Select a smaller page range.")
                sheet = workbook.create_sheet(f"Page {index + 1} table {number}")
                for row in rows:
                    sheet.append([(value or "")[:32767] for value in row])
                    for cell in sheet[sheet.max_row]:
                        cell.data_type = "s"
                sheet.freeze_panes = "A2"
    if not workbook.worksheets:
        raise JobError("No usable tables were detected. Scanned tables need OCR before extraction.")
    workbook.save(output)


def pdf_to_docx(path, pages, output):
    import pdfplumber
    from docx import Document
    from docx.shared import Inches, Pt

    document = Document()
    section = document.sections[0]
    section.left_margin = section.right_margin = Inches(0.65)
    document.styles["Normal"].font.size = Pt(10)
    scanned = []
    text_pages = 0
    with pdfplumber.open(path, unicode_norm="NFC") as pdf:
        for index in pages:
            page = pdf.pages[index]
            visible_tables = list(page.find_tables()[:12])
            has_text = bool(page.chars)
            if has_text:
                text_pages += 1
                # Interleave text and ruled tables in reading order.
                events = [(table.bbox[1], "table", table.extract()) for table in visible_tables]
                words = [
                    word for word in page.extract_words()
                    if not any(x0 <= (word["x0"] + word["x1"]) / 2 <= x1
                               and top <= (word["top"] + word["bottom"]) / 2 <= bottom
                               for x0, top, x1, bottom in (table.bbox for table in visible_tables))
                ]
                lines = []
                for word in words:
                    if not lines or abs(lines[-1][0] - word["top"]) > 3:
                        lines.append((word["top"], []))
                    lines[-1][1].append(word["text"])
                events.extend((top, "text", " ".join(line)) for top, line in lines)
                for _, kind, value in sorted(events, key=lambda event: event[0]):
                    if kind == "text":
                        document.add_paragraph(value)
                    elif value and value[0]:
                        if len(value) > 200 or len(value[0]) > 30:
                            continue
                        table = document.add_table(rows=0, cols=len(value[0]))
                        table.style = "Table Grid"
                        for row in value:
                            cells = table.add_row().cells
                            for cell, text in zip(cells, row):
                                cell.text = text or ""
            # A full-page visual is used for scans; mixed text/image pages retain a visual reference.
            if not has_text or page.images:
                if not has_text:
                    scanned.append(index)
                with rendered_pages(path, [index], 90) as pictures:
                    _, image = next(pictures)
                    with image, io.BytesIO() as stream:
                        image.convert("RGB").save(stream, "JPEG", quality=82)
                        stream.seek(0)
                        width = min(6.7, 8.3 * image.width / image.height)
                        document.add_picture(stream, width=Inches(width))
            if index != pages[-1]:
                document.add_page_break()
    document.save(output)
    if not text_pages:
        return "Scanned PDF. Pages are images in Word. OCR is not enabled."
    if scanned:
        return "Editable text and detected tables were reconstructed. Scanned pages are images. Layout may differ."
    return "Editable text and detected tables were reconstructed. Complex layouts may differ."


def pdf_to_pptx(path, pages, output, dpi):
    import pypdfium2 as pdfium
    from pptx import Presentation
    from pptx.util import Inches

    slides = Presentation()
    document = pdfium.PdfDocument(path)
    try:
        first = document[pages[0]]
        try:
            width, height = first.get_size()
        finally:
            first.close()
    finally:
        document.close()
    if width <= 0 or height <= 0:
        raise JobError("Invalid PDF page dimensions.")
    ratio = max(0.25, min(4, width / height))
    slides.slide_width = Inches(10 if ratio >= 1 else 10 * ratio)
    slides.slide_height = Inches(10 / ratio if ratio >= 1 else 10)

    with rendered_pages(path, pages, dpi) as pictures:
        for _, image in pictures:
            with image, io.BytesIO() as stream:
                image.convert("RGB").save(stream, "JPEG", quality=88)
                stream.seek(0)
                scale = min(slides.slide_width / image.width, slides.slide_height / image.height)
                width, height = int(scale * image.width), int(scale * image.height)
                slide = slides.slides.add_slide(slides.slide_layouts[6])
                slide.shapes.add_picture(stream, (slides.slide_width - width) // 2,
                                         (slides.slide_height - height) // 2, width, height)
    slides.save(output)


def pdf_to_office(path, directory, action, settings):
    reader, _ = pdf_reader(path)
    pages = page_indices(settings.get("pages", "all"), len(reader.pages))
    if len(pages) > 20:
        raise JobError("Select at most 20 pages for this conversion.")
    target = Path(directory) / ("converted." + action)
    if action == "docx":
        note = pdf_to_docx(path, pages, target)
    elif action == "xlsx":
        pdf_to_xlsx(path, pages, target)
        note = "Detected PDF tables were extracted. Original Excel formulas are not recoverable."
    else:
        pdf_to_pptx(path, pages, target, settings.get("dpi", 120))
        note = "Each slide is an image of one PDF page."
    return {"file": target.name, "note": note}
