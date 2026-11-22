from html import escape

from document_native import html_to_pdf, inspect_office_archive
from pdf_tools import JobError

BASIC_OFFICE_EXTENSIONS = {"docx", "xlsx"}
MAX_ROWS = 500
MAX_COLUMNS = 12
MAX_TEXT = 120_000
MAX_CELL_TEXT = 2_000
MAX_PARAGRAPHS = 250


class Markup:
    def __init__(self):
        self.text_size = 0
        self.rows = 0

    def text(self, value, limit=MAX_CELL_TEXT):
        text = "" if value is None else str(value)
        self.text_size += len(text)
        if len(text) > limit or self.text_size > MAX_TEXT:
            raise JobError("There is too much text for Basic PDF. Try a smaller document.")
        return escape(text)

    def row(self, values):
        self.rows += 1
        if self.rows > MAX_ROWS or len(values) > MAX_COLUMNS:
            raise JobError("Basic PDF supports up to 500 table rows and 12 columns.")
        return "<tr>" + "".join(f"<td>{self.text(value)}</td>" for value in values) + "</tr>"


def render_word(source, markup):
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    parts = []
    paragraphs = 0
    document = Document(source)
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph) and block.text.strip():
            paragraphs += 1
            if paragraphs > MAX_PARAGRAPHS:
                raise JobError("Basic PDF supports up to 250 Word paragraphs.")
            style = block.style.name if block.style is not None else ""
            tag = "h2" if style == "Title" or style.startswith("Heading ") else "p"
            parts.append(f"<{tag}>{markup.text(block.text, 8_000)}</{tag}>")
        elif isinstance(block, Table):
            if len(block.columns) > MAX_COLUMNS:
                raise JobError("Basic PDF supports up to 12 table columns.")
            rows = [markup.row(tuple(cell.text for cell in row.cells)) for row in block.rows]
            if rows:
                parts.extend(["<table>", *rows, "</table>"])
    if not parts:
        raise JobError("No Word text or tables were found. Images are omitted by Basic PDF.")
    return "".join(parts)


def render_excel(source, markup):
    from openpyxl import load_workbook

    sections = []
    workbook = load_workbook(source, read_only=True, data_only=True, keep_links=False)
    try:
        sheets = [sheet for sheet in workbook.worksheets if sheet.sheet_state == "visible"]
        if len(sheets) > 10:
            raise JobError("Basic PDF supports up to 10 visible Excel sheets.")
        for sheet in sheets:
            # Read actual records even when producers write inaccurate dimensions.
            sheet.reset_dimensions()
            rows = []
            for number, values in enumerate(sheet.iter_rows(values_only=True), 1):
                if number > MAX_ROWS:
                    raise JobError("Basic PDF supports up to 500 rows per Excel sheet.")
                used = len(values)
                while used and values[used - 1] is None:
                    used -= 1
                if used:
                    rows.append(markup.row(values[:used]))
            if rows:
                sections.append(f"<section><h2>{markup.text(sheet.title)}</h2><table>"
                                + "".join(rows) + "</table></section>")
    finally:
        workbook.close()
    if not sections:
        raise JobError("No visible saved Excel values were found. Recalculate and save formula cells in Excel before uploading.")
    return "".join(sections)


def office_to_basic_pdf(source, directory):
    extension = source.suffix[1:]
    if extension not in BASIC_OFFICE_EXTENSIONS:
        raise JobError("Basic PDF accepts DOCX and XLSX files.", 415)
    inspect_office_archive(source, extension)
    markup = Markup()
    excel = extension == "xlsx"
    body = render_excel(source, markup) if excel else render_word(source, markup)
    size = "A4 landscape" if excel else "A4"
    html = directory / "basic.html"
    html.write_text(
        "<!doctype html><html><meta charset='utf-8'><style>"
        + f"@page{{size:{size};margin:14mm}}"
        + "body{font-family:sans-serif;font-size:10pt;overflow-wrap:anywhere}"
          "p,td,h2{white-space:pre-wrap}p{margin:0 0 6pt}h2{break-after:avoid}"
          "table{width:100%;table-layout:fixed;border-collapse:collapse;margin-bottom:10pt}"
          "td{padding:3pt;border:1px solid #999;vertical-align:top}tr{break-inside:avoid}"
          "section + section{break-before:page}</style><body>"
        + body + "</body></html>",
        encoding="utf-8",
    )
    output = directory / "converted.pdf"
    html_to_pdf(html, output)
    note = "Basic PDF with saved cell values. Formulas were not recalculated. Charts and formatting are omitted." if excel else (
        "Basic PDF with Word text and tables. Images and exact formatting are omitted."
    )
    return {"file": output.name, "note": note}
