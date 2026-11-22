import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
import subprocess

from docx import Document
from httpx import ASGITransport, AsyncClient, ByteStream, MockTransport, Response
from openpyxl import Workbook, load_workbook
from PIL import Image
from pptx import Presentation
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from document_native import inspect_office_archive, validate_pdfa_report
from pdf_tools import JobError
import document_routes
import document_native
import native_relay
import main


def sample_pdf(table=False, pages=3):
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer, pagesize=(300, 240))
    for number in range(1, pages + 1):
        document.drawString(30, 195, f"Synthetic page {number}")
        if table:
            for x in (30, 120, 210):
                document.line(x, 70, x, 145)
            for y in (70, 95, 120, 145):
                document.line(30, y, 210, y)
            for text, x, y in (("Name", 40, 129), ("Value", 130, 129), ("Alpha", 40, 104),
                               ("=2+3", 130, 104), ("Beta", 40, 79), ("42", 130, 79)):
                document.drawString(x, y, text)
        document.showPage()
    document.save()
    return buffer.getvalue()


class DocumentAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        key = patch.object(main, "API_KEY", "test-key")
        key.start()
        self.addCleanup(key.stop)
        self.client = AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def convert(self, action, files=None, options=None, password=None, key="test-key"):
        if files is None:
            files = [("source.pdf", sample_pdf())]
        data = {"action": action, "options": json.dumps(options or {})}
        if password is not None:
            data["password"] = password
        return await self.client.post(
            "/convert-document", headers={"x-api-key": key}, data=data,
            files=[("file", (name, content, "application/octet-stream")) for name, content in files],
        )

    def read_pdf(self, result):
        self.assertEqual(result.status_code, 200, result.text[:200] if result.is_error else "")
        return PdfReader(io.BytesIO(result.content))

class DocumentTests(DocumentAPI):
    async def test_pdf_render_ranged_zip_and_first_page(self):
        first = await self.convert("jpg", options={"dpi": 72})
        self.assertEqual(first.status_code, 200)
        with Image.open(io.BytesIO(first.content)) as picture:
            self.assertEqual((picture.size, picture.format), ((300, 240), "JPEG"))
        result = await self.convert("png", options={"pages": "2-3", "dpi": 144})
        self.assertEqual(result.status_code, 200, result.text[:200] if result.is_error else "")
        with ZipFile(io.BytesIO(result.content)) as archive:
            self.assertEqual(archive.namelist(), ["page-002.png", "page-003.png"])
            with Image.open(io.BytesIO(archive.read("page-002.png"))) as picture:
                self.assertEqual((picture.size, picture.format), ((600, 480), "PNG"))

    async def test_page_operations_and_metadata(self):
        merged = self.read_pdf(await self.convert("merge", files=[
            ("first.pdf", sample_pdf(pages=2)), ("second.pdf", sample_pdf(pages=1)),
        ]))
        self.assertEqual([page.extract_text().strip() for page in merged.pages],
                         ["Synthetic page 1", "Synthetic page 2", "Synthetic page 1"])
        extract = self.read_pdf(await self.convert("extract", options={"pages": "2"}))
        self.assertEqual(extract.pages[0].extract_text().strip(), "Synthetic page 2")
        deleted = self.read_pdf(await self.convert("delete", options={"pages": "2"}))
        self.assertEqual(len(deleted.pages), 2)
        rotate = self.read_pdf(await self.convert("rotate", options={"pages": "1,3", "angle": 270}))
        self.assertEqual([page.rotation for page in rotate.pages], [270, 0, 270])
        result = await self.convert("split", options={"pages": "1-2"})
        self.assertEqual(result.status_code, 200)
        with ZipFile(io.BytesIO(result.content)) as archive:
            self.assertEqual(len(PdfReader(io.BytesIO(archive.read("selected.pdf"))).pages), 2)
            self.assertEqual(len(PdfReader(io.BytesIO(archive.read("remaining.pdf"))).pages), 1)
        meta = await self.convert("inspect")
        self.assertEqual(meta.json()["pages"], 3)
        self.assertEqual(meta.json()["page_width_pt"], 300)
        for op, pages in [("split", "all"), ("delete", "all"), ("extract", "4")]:
            self.assertEqual((await self.convert(op, options={"pages": pages})).status_code, 422)

    async def test_telegram_numbered_pdf_merge_preserves_order(self):
        one = ("one.pdf", sample_pdf(pages=1), "application/pdf")
        two = ("two.pdf", sample_pdf(pages=2), "application/pdf")
        response = await self.client.post(
            "/convert-document", headers={"x-api-key": "test-key"},
            files=[("file_2", two), ("file", one)], data={"action": "merge"},
        )
        pages = self.read_pdf(response).pages
        self.assertEqual([page.extract_text().strip() for page in pages],
                         ["Synthetic page 1", "Synthetic page 1", "Synthetic page 2"])

    async def test_compression_preserves_text(self):
        source = io.BytesIO()
        document = canvas.Canvas(source, pageCompression=0)
        for index in range(60):
            document.drawString(20, 810 - 12 * index, "Compress this repeated fixture text")
        document.save()
        data = source.getvalue()
        for level in ("low", "balanced", "high"):
            result = await self.convert("compress", [("source.pdf", data)], {"level": level})
            converted = self.read_pdf(result)
            self.assertEqual(converted.pages[0].extract_text().count("repeated fixture text"), 60)
            self.assertLess(len(result.content), len(data))

    async def test_unlock_wrong_user_and_owner_password(self):
        buffer = io.BytesIO()
        with PdfWriter() as writer:
            writer.append_pages_from_reader(PdfReader(io.BytesIO(sample_pdf())))
            writer.encrypt("fixture-user", "fixture-owner", algorithm="AES-256")
            writer.write(buffer)
        files = [("encrypted.pdf", buffer.getvalue())]
        self.assertEqual((await self.convert("inspect", files)).status_code, 423)
        self.assertEqual((await self.convert("unlock", files)).status_code, 400)
        wrong = await self.convert("unlock", files, password="incorrect-fixture")
        self.assertEqual(wrong.status_code, 403)
        self.assertNotIn("incorrect-fixture", wrong.text)
        for password in ("fixture-user", "fixture-owner"):
            result = await self.convert("unlock", files, password=password)
            self.assertFalse(self.read_pdf(result).is_encrypted)

    async def test_pdf_to_office_outputs(self):
        files = [("source.pdf", sample_pdf(table=True, pages=2))]
        docx = await self.convert("docx", files, {"pages": "first"})
        self.assertEqual(docx.status_code, 200, docx.text[:200] if docx.is_error else "")
        word = Document(io.BytesIO(docx.content))
        self.assertIn("Synthetic page 1", [item.text for item in word.paragraphs])
        self.assertEqual(word.tables[0].cell(1, 0).text, "Alpha")
        xlsx = await self.convert("xlsx", files)
        self.assertEqual(xlsx.status_code, 200, xlsx.text[:200] if xlsx.is_error else "")
        with io.BytesIO(xlsx.content) as spreadsheet:
            workbook = load_workbook(spreadsheet)
            self.assertEqual(workbook.sheetnames, ["Page 1 table 1", "Page 2 table 1"])
            self.assertEqual((workbook.active["B2"].value, workbook.active["B2"].data_type), ("=2+3", "s"))
            workbook.close()
        self.assertEqual((await self.convert("xlsx")).status_code, 422)
        pptx = await self.convert("pptx", files, {"dpi": 72})
        self.assertEqual(pptx.status_code, 200, pptx.text[:200] if pptx.is_error else "")
        presentation = Presentation(io.BytesIO(pptx.content))
        self.assertEqual([len(slide.shapes) for slide in presentation.slides], [1, 1])
        self.assertAlmostEqual(presentation.slide_width / presentation.slide_height, 1.25)

    async def test_html_is_offline_and_scripts_are_inert(self):
        html = b"<html><body><h1>Fixture title</h1><script>document.write('not visible')</script></body></html>"
        result = self.read_pdf(await self.convert("html", [("source.html", html)]))
        self.assertIn("Fixture title", result.pages[0].extract_text())
        self.assertNotIn("not visible", result.pages[0].extract_text())
        external = await self.convert("html", [("source.html", b'<img src="https://example.invalid/image.png">')])
        self.assertEqual(external.status_code, 422)
        self.assertIn("external resources", external.text)

    async def test_basic_word_pdf_preserves_text_and_table_order(self):
        word = Document()
        word.add_heading("Project notes", level=1)
        word.add_table(rows=1, cols=1).cell(0, 0).text = "<script>literal</script>"
        word.add_paragraph("Review complete")
        fixture = io.BytesIO()
        word.save(fixture)
        with patch.object(document_routes, "native_features", return_value={"office": False, "pdfa": False}):
            caps = (await self.client.get("/document-capabilities")).json()
            self.assertTrue(caps["office_basic"])
            self.assertFalse(caps["office"])
            response = await self.convert("office-basic", [("source.docx", fixture.getvalue())])
        output = " ".join(self.read_pdf(response).pages[0].extract_text().split())
        self.assertEqual(output, "Project notes <script>literal</script> Review complete")
        self.assertIn("Word text and tables", response.headers["x-conversion-note"])

    async def test_basic_excel_pdf_uses_saved_values_and_visible_sheets(self):
        workbook = Workbook()
        workbook.active.title = "Visible figures"
        workbook.active.append(["Revenue", 175, "=6*7", "=2+3"])
        workbook.create_sheet("Hidden sheet").sheet_state = "hidden"
        workbook["Hidden sheet"]["A1"] = "Hidden content"
        fixture = io.BytesIO()
        workbook.save(fixture)
        cached = io.BytesIO()
        with ZipFile(fixture) as original, ZipFile(cached, "w") as edited:
            for name in original.namelist():
                data = original.read(name)
                if name == "xl/worksheets/sheet1.xml":
                    data = data.replace(b"<f>6*7</f><v></v>", b"<f>6*7</f><v>42</v>")
                edited.writestr(name, data)
        response = await self.convert("office-basic", [("source.xlsx", cached.getvalue())])
        output = " ".join(self.read_pdf(response).pages[0].extract_text().split())
        self.assertEqual(output, "Visible figures Revenue 175 42")
        self.assertIn("saved cell values", response.headers["x-conversion-note"])

    async def test_basic_office_rejects_unsupported_format_and_wide_sheets(self):
        unsupported = await self.convert("office-basic", [("source.pptx", b"fixture")])
        self.assertEqual(unsupported.status_code, 415)
        workbook = Workbook()
        workbook.active["M1"] = "Beyond the width limit"
        fixture = io.BytesIO()
        workbook.save(fixture)
        wide = await self.convert("office-basic", [("source.xlsx", fixture.getvalue())])
        self.assertEqual(wide.status_code, 422)
        self.assertIn("12 columns", wide.text)

    async def test_auth_invalid_input_native_feature_gates_and_timeout(self):
        self.assertEqual((await self.convert("inspect", key="wrong")).status_code, 401)
        self.assertEqual((await self.convert("inspect", [("source.pdf", b"invalid")])).status_code, 415)
        self.assertEqual((await self.convert("inspect", [("source.txt", sample_pdf())])).status_code, 415)
        self.assertEqual((await self.convert("inspect", [("source.pdf", b"x" * 20_000_001)])).status_code, 413)
        with patch.object(document_routes, "native_features", return_value={"office": False, "pdfa": False}):
            self.assertEqual((await self.convert("office", [("source.docx", b"fixture")])).status_code, 503)
            self.assertEqual((await self.convert("pdfa")).status_code, 503)
        with tempfile.TemporaryDirectory() as root, patch.object(tempfile, "tempdir", root):
            with patch.object(document_routes, "process_document", side_effect=TimeoutError):
                self.assertEqual((await self.convert("inspect")).status_code, 504)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_pdfa_validation_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            report = Path(root) / "report.xml"
            for data in ('<report/>', '<report><validationReport isCompliant="false"/></report>'):
                report.write_text(data)
                with self.assertRaises(JobError):
                    validate_pdfa_report(report)
            report.write_text('<report><validationReport profileName="PDF/A-2b validation profile" isCompliant="true"/></report>')
            validate_pdfa_report(report)


    async def test_image_compression_and_scanned_word_fallback(self):
        noise = Image.effect_noise((1200, 1200), 75)
        raw = io.BytesIO()
        noise.save(raw, "PDF")
        files = [("scanned.pdf", raw.getvalue())]
        result = await self.convert("compress", files, {"level": "high"})
        self.assertEqual(len(self.read_pdf(result).pages), 1)
        self.assertLess(len(result.content), len(raw.getvalue()))
        word = await self.convert("docx", files, {"pages": "first"})
        self.assertEqual(word.status_code, 200, word.text[:200] if word.is_error else "")
        self.assertIn("OCR is not enabled", word.headers["x-conversion-note"])
        self.assertEqual(len(Document(io.BytesIO(word.content)).inline_shapes), 1)

    def test_office_archive_validation(self):
        with tempfile.TemporaryDirectory() as root:
            sample = Path(root) / "source.docx"
            document = Document()
            document.add_paragraph("Synthetic Office input")
            document.save(sample)
            inspect_office_archive(sample, "docx")
            with self.assertRaises(JobError):
                inspect_office_archive(sample, "xlsx")
            with ZipFile(sample, "a") as archive:
                archive.writestr("word/embeddings/object.bin", b"fixture")
            with self.assertRaises(JobError):
                inspect_office_archive(sample, "docx")


    @unittest.skipUnless(os.getenv("RUN_NATIVE_DOCUMENT_TESTS") == "1", "requires a native sandbox host")
    async def test_native_office_to_pdf(self):
        from pptx.util import Inches

        self.assertTrue((await self.client.get("/document-capabilities")).json()["office"])
        word = Document()
        word.add_paragraph("Synthetic Word document")
        word.add_table(rows=1, cols=2).rows[0].cells[1].text = "Word table value"
        word.add_page_break()
        word.add_paragraph("Second Word page")

        excel = Workbook()
        excel.active["A1"] = "Synthetic Excel worksheet"
        excel.active["B2"] = "=6*7"
        excel.active["A20"] = "Outside the print area"
        excel.active.print_area = "A1:B2"
        excel.active.page_setup.orientation = "landscape"

        powerpoint = Presentation()
        powerpoint.slide_width = Inches(12)
        powerpoint.slide_height = Inches(6.75)
        for number in (1, 2):
            slide = powerpoint.slides.add_slide(powerpoint.slide_layouts[6])
            textbox = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
            textbox.text = f"Synthetic PowerPoint slide {number}"

        outputs = {}
        for suffix, fixture in (("docx", word), ("xlsx", excel), ("pptx", powerpoint)):
            stream = io.BytesIO()
            fixture.save(stream)
            result = await self.convert("office", [("source." + suffix, stream.getvalue())])
            outputs[suffix] = self.read_pdf(result)

        word_pages = outputs["docx"].pages
        self.assertEqual(len(word_pages), 2)
        self.assertIn("Word table value", word_pages[0].extract_text())
        self.assertIn("Second Word page", word_pages[1].extract_text())

        excel_pages = outputs["xlsx"].pages
        self.assertEqual(len(excel_pages), 1)
        excel_page = excel_pages[0]
        self.assertGreater(excel_page.mediabox.width, excel_page.mediabox.height)
        excel_text = excel_page.extract_text()
        self.assertIn("Synthetic Excel worksheet", excel_text)
        self.assertIn("42", excel_text)
        self.assertNotIn("Outside the print area", excel_text)

        slides = outputs["pptx"].pages
        self.assertEqual(len(slides), 2)
        for number, page in enumerate(slides, 1):
            self.assertIn(f"Synthetic PowerPoint slide {number}", page.extract_text())
            self.assertAlmostEqual(float(page.mediabox.width / page.mediabox.height), 16 / 9, places=2)

    @unittest.skipUnless(os.getenv("RUN_PDFA_TESTS") == "1", "requires Ghostscript and veraPDF in the sandbox")
    async def test_native_archive_pdfa(self):
        self.assertTrue((await self.client.get("/document-capabilities")).json()["pdfa"])
        result = await self.convert("pdfa")
        self.assertIn("verified by veraPDF", result.headers.get("x-conversion-note", ""))
        pdf = self.read_pdf(result)
        self.assertIn("Synthetic page 1", pdf.pages[0].extract_text())
        self.assertIn("/OutputIntents", pdf.root_object)


class NativeReadinessTests(unittest.TestCase):
    def setUp(self):
        document_native.native_features.cache_clear()
        self.addCleanup(document_native.native_features.cache_clear)

    def test_reports_activation_and_missing_packages(self):
        with patch.dict(os.environ, {"CONVERTER_ENABLE_NATIVE_DOCUMENTS": "0"}):
            self.assertEqual(document_native.native_features(), {
                "office": False, "pdfa": False, "native_status": "activation_disabled",
            })
        document_native.native_features.cache_clear()
        with patch.dict(os.environ, {"CONVERTER_ENABLE_NATIVE_DOCUMENTS": "1"}), patch.object(Path, "is_file", return_value=False):
            self.assertEqual(document_native.native_features(), {
                "office": False, "pdfa": False, "native_status": "packages_missing",
            })

    def test_sandbox_failure_is_sanitized_and_keeps_conversions_disabled(self):
        completed = subprocess.CompletedProcess([], 1, stderr="bwrap: Creating new namespace: Operation not permitted")
        with patch.dict(os.environ, {"CONVERTER_ENABLE_NATIVE_DOCUMENTS": "1"}), patch.object(Path, "is_file", return_value=True), patch.object(document_native.subprocess, "run", return_value=completed):
            self.assertEqual(document_native.native_features(), {
                "office": False, "pdfa": False, "native_status": "sandbox_denied",
            })

    def test_optional_tools_are_available_after_successful_isolation(self):
        completed = subprocess.CompletedProcess([], 0, stderr="")
        with patch.dict(os.environ, {"CONVERTER_ENABLE_NATIVE_DOCUMENTS": "1"}), patch.object(Path, "is_file", return_value=True), patch.object(document_native.subprocess, "run", return_value=completed):
            self.assertEqual(document_native.native_features(), {
                "office": True, "pdfa": True, "native_status": "ready",
            })


class RemoteNativeWorkerTests(DocumentAPI):
    async def test_remote_capabilities_use_only_worker_native_status(self):
        def upstream(request):
            self.assertEqual(request.url.path, "/native-worker-capabilities")
            self.assertEqual(request.headers["x-api-key"], "test-worker-secret")
            return Response(200, stream=ByteStream(b'{"office":true,"pdfa":true}'))

        with self.configure(upstream):
            response = await self.client.get("/document-capabilities")
            self.assertEqual(response.json()["native_status"], "remote_ready")
            self.assertTrue(response.json()["office"])
            rejected = await self.client.get("/native-worker-capabilities")
            self.assertEqual(rejected.status_code, 401)
            local = await self.client.get("/native-worker-capabilities", headers={"x-api-key": "test-key"})
            self.assertFalse(local.json()["office"])

    async def test_remote_office_and_pdfa_require_expected_pdf_response(self):
        archive_verified = False
        calls = []
        pdf = sample_pdf(pages=1)

        async def upstream(request):
            self.assertEqual(request.url.path, "/convert-document")
            self.assertEqual(request.headers["x-api-key"], "test-worker-secret")
            self.assertEqual(request.headers[native_relay.LOCAL_ONLY_HEADER], "1")
            payload = await request.aread()
            calls.append(payload)
            headers = {"content-type": "application/pdf"}
            if archive_verified:
                headers["x-conversion-note"] = native_relay.ARCHIVE_NOTE
            return Response(200, headers=headers, stream=ByteStream(pdf))

        with self.configure(upstream):
            office = await self.convert("office", [("source.docx", b"fixture")])
            self.assertEqual(len(self.read_pdf(office).pages), 1)
            self.assertIn(b'filename="input-0.docx"', calls[0])
            archive = await self.convert("pdfa")
            self.assertEqual(archive.status_code, 502)
            archive_verified = True
            archive = await self.convert("pdfa")
            self.assertEqual(len(self.read_pdf(archive).pages), 1)
            self.assertEqual(archive.headers["x-conversion-note"], native_relay.ARCHIVE_NOTE)

    async def test_native_only_request_cannot_loop_or_forward_standard_operations(self):
        def unexpected(request):
            self.fail("A local-only or standard document request must never be relayed.")

        with self.configure(unexpected):
            local = await self.client.post(
                "/convert-document", data={"action": "pdfa"},
                headers={"x-api-key": "test-key", native_relay.LOCAL_ONLY_HEADER: "1"},
                files={"file": ("source.pdf", sample_pdf(), "application/pdf")},
            )
            self.assertEqual(local.status_code, 503)
            self.assertEqual((await self.convert("inspect")).status_code, 200)

    def configure(self, handler):
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, {
            "NATIVE_WORKER_URL": "https://native.example.test",
            "NATIVE_WORKER_API_KEY": "test-worker-secret",
        }))
        stack.enter_context(patch.object(document_routes, "native_features", return_value={
            "office": False, "pdfa": False, "native_status": "sandbox_denied",
        }))
        stack.enter_context(patch.object(native_relay, "client", side_effect=lambda timeout: AsyncClient(
            timeout=timeout, transport=MockTransport(handler), follow_redirects=False, trust_env=False,
        )))
        return stack
