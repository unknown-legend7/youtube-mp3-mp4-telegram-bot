import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from PIL import Image
from pypdf import PdfReader

import image_routes
import main


def fixture(fmt="PNG", size=(320, 200), color="red", mode="RGB", **options):
    stream = io.BytesIO()
    Image.new(mode, size, color).save(stream, fmt, **options)
    return stream.getvalue()


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        key = patch.object(main, "API_KEY", "test-key")
        key.start()
        self.addCleanup(key.stop)
        self.client = AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def convert(self, files, fmt, options=None, key="test-key"):
        return await self.client.post(
            "/convert-image", headers={"x-api-key": key},
            files=[("file", (name, data, "application/octet-stream")) for name, data in files],
            data={"output_format": fmt, "options": json.dumps(options or {})},
        )

    def output_image(self, response, fmt):
        self.assertEqual(response.status_code, 200, response.text[:200] if response.is_error else "")
        image = Image.open(io.BytesIO(response.content))
        image.load()
        self.assertEqual(image.format, fmt)
        self.addCleanup(image.close)
        return image

    async def test_format_matrix(self):
        inputs = {"jpg": ("JPEG", ["png", "webp"]), "jpeg": ("JPEG", ["png", "webp"]),
                  "png": ("PNG", ["jpg", "jpeg", "webp"]), "webp": ("WEBP", ["jpg", "png"]),
                  "bmp": ("BMP", ["jpg", "png"]), "tiff": ("TIFF", ["jpg", "png"])}
        names = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}
        for source, (source_format, targets) in inputs.items():
            data = fixture(source_format)
            for target in targets:
                with self.subTest(source=source, target=target):
                    response = await self.convert([("source." + source, data)], target)
                    self.assertEqual(self.output_image(response, names[target]).size, (320, 200))

    async def test_orientation_metadata_and_transparency(self):
        exif = Image.Exif()
        exif[274] = 6
        exif[315] = "Private author"
        jpeg = fixture("JPEG", exif=exif)
        converted = self.output_image(await self.convert([("source.jpg", jpeg)], "png"), "PNG")
        self.assertEqual(converted.size, (200, 320))
        self.assertFalse(converted.getexif())
        alpha = fixture(mode="RGBA", color=(0, 0, 0, 0))
        png = self.output_image(await self.convert([("source.png", alpha)], "png"), "PNG")
        self.assertEqual(png.getpixel((2, 2))[3], 0)
        for background, expected in (("white", (255, 255, 255)), ("black", (0, 0, 0))):
            result = await self.convert([("source.png", alpha)], "jpg", {"background": background})
            self.assertEqual(self.output_image(result, "JPEG").getpixel((2, 2)), expected)

    async def test_compress_resize_and_target_reporting(self):
        random_image = Image.effect_noise((640, 480), 55)
        stream = io.BytesIO()
        random_image.save(stream, "PNG")
        data = stream.getvalue()
        files = [("source.png", data)]
        result = await self.convert(files, "jpg", {"quality": 55, "compress": True, "target_kb": 1})
        self.assertEqual(self.output_image(result, "JPEG").size, (640, 480))
        self.assertLess(len(result.content), len(data))
        self.assertEqual(result.headers["x-input-size"], str(len(data)))
        self.assertEqual(result.headers["x-target-met"], "false")
        for options, size in [
            ({"mode": "width", "width": 320}, (320, 240)),
            ({"mode": "height", "height": 120}, (160, 120)),
            ({"mode": "percent", "percent": 50}, (320, 240)),
            ({"mode": "exact", "width": 420, "height": 100}, (420, 100)),
            ({"mode": "fit", "width": 1280, "height": 240}, (320, 240)),
            ({"mode": "width", "width": 800, "upscale": True}, (800, 600)),
        ]:
            with self.subTest(options=options):
                result = await self.convert(files, "png", options)
                self.assertEqual(self.output_image(result, "PNG").size, size)
        result = await self.convert(files, "png", {"mode": "width", "width": 800})
        self.assertEqual(result.status_code, 422)

    async def test_pdf_page_order_layout_and_formats(self):
        files = [("a.jpg", fixture("JPEG", (320, 200), "red")),
                 ("b.png", fixture("PNG", (100, 200), "blue")),
                 ("c.webp", fixture("WEBP", (200, 200), "green"))]
        response = await self.convert(files, "pdf", {"page": "a4", "orientation": "landscape", "margin": 24})
        self.assertEqual(response.status_code, 200)
        reader = PdfReader(io.BytesIO(response.content))
        self.assertEqual(len(reader.pages), 3)
        for index, channel in enumerate([0, 2, 1]):
            page = reader.pages[index]
            self.assertAlmostEqual(float(page.mediabox.width), 841.89, places=1)
            self.assertAlmostEqual(float(page.mediabox.height), 595.28, places=1)
            image = next(iter(page.images)).image
            samples = image.convert("RGB").getpixel((20, 20))
            self.assertEqual(samples.index(max(samples)), channel)
        for page, size in [("fit", (240, 150)), ("letter", (792, 612))]:
            result = await self.convert(files[:1], "pdf", {"page": page})
            first = PdfReader(io.BytesIO(result.content)).pages[0]
            self.assertEqual((float(first.mediabox.width), float(first.mediabox.height)), size)

    async def test_telegram_batch_fields_are_ordered_and_bounded(self):
        blue = ("blue.png", fixture(color="blue"), "image/png")
        red = ("red.png", fixture(color="red"), "image/png")
        yellow = ("yellow.png", fixture(color="yellow"), "image/png")
        async def request(files, output):
            return await self.client.post(
                "/convert-image", headers={"x-api-key": "test-key"},
                files=files, data={"output_format": output},
            )
        result = await request([("file_3", yellow), ("file", blue), ("file_2", red)], "pdf")
        self.assertEqual(result.status_code, 200)
        reader = PdfReader(io.BytesIO(result.content))
        self.assertEqual(len(reader.pages), 3)
        for page, color in zip(reader.pages, ((0, 0, 255), (255, 0, 0), (255, 255, 0))):
            pixel = next(iter(page.images)).image.convert("RGB").getpixel((5, 5))
            for actual, expected in zip(pixel, color):
                self.assertAlmostEqual(actual, expected, delta=5)
        for files, output in [
            ([("file", blue), ("file_3", yellow)], "pdf"),
            ([("file", blue), ("file_2", red), ("file_2", yellow)], "pdf"),
            ([("file", blue), ("file", red), ("file_2", yellow)], "pdf"),
            ([("file", blue), ("file_2", red)], "png"),
        ]:
            with self.subTest(output=output, fields=[name for name, _ in files]):
                self.assertEqual((await request(files, output)).status_code, 400)

    async def test_invalid_image_animation_auth_and_limits(self):
        png = fixture()
        with io.BytesIO() as stream:
            first = Image.new("RGB", (100, 100), "red")
            second = Image.new("RGB", (100, 100), "blue")
            first.save(stream, "WEBP", save_all=True, append_images=[second], duration=80)
            animated = stream.getvalue()
        for files, expected in [
            ([("source.png", b"bad image")], 422),
            ([("source.jpg", png)], 422),
            ([("source.svg", png)], 415),
            ([("source.webp", animated)], 422),
            ([("source.png", fixture(size=(3000, 3000)))], 422),
            ([("source.png", b"0" * 20_000_001)], 413),
        ]:
            self.assertEqual((await self.convert(files, "png")).status_code, expected)
        self.assertEqual((await self.convert([("source.png", png)], "png", key="bad")).status_code, 401)

    async def test_cleanup_on_success_and_timeout(self):
        files = [("source.png", fixture())]
        with tempfile.TemporaryDirectory() as root, patch.object(tempfile, "tempdir", root):
            self.assertEqual((await self.convert(files, "png")).status_code, 200)
            self.assertEqual(list(Path(root).iterdir()), [])
            with patch.object(image_routes, "process_image", side_effect=TimeoutError):
                self.assertEqual((await self.convert(files, "png")).status_code, 504)
            self.assertEqual(list(Path(root).iterdir()), [])
