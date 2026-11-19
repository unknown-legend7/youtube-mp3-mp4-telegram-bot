import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from httpx import ASGITransport, AsyncClient
import main


class ConversionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=320x240",
                "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1",
                "-c:v", "libx264", "-c:a", "aac", str(path),
            ], check=True)
            cls.source = path.read_bytes()

    async def asyncSetUp(self):
        self.original_key = main.API_KEY
        main.API_KEY = "local-test-key"
        self.client = AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test")
        self.addCleanup(setattr, main, "API_KEY", self.original_key)
        self.addAsyncCleanup(self.client.aclose)

    async def convert(self, fmt, payload=None, key="local-test-key", mime="video/mp4"):
        return await self.client.post(
            "/convert", headers={"x-api-key": key},
            files={"file": ("source.mp4", self.source if payload is None else payload, mime)},
            data={"output_format": fmt, "audio_bitrate": "64k", "video_resolution": "1080p"},
        )

    async def test_mp3_and_mp4(self):
        for fmt, mime, codec in (("mp3", "audio/mpeg", b"mp3"), ("mp4", "video/mp4", b"h264")):
            with self.subTest(fmt=fmt):
                result = await self.convert(fmt)
                self.assertEqual(result.status_code, 200, result.text[:200] if not result.is_success else "")
                self.assertEqual(result.headers["content-type"], mime)
                probe = subprocess.run([
                    "ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height",
                    "-of", "default=noprint_wrappers=1", "pipe:0",
                ], input=result.content, capture_output=True, check=True).stdout
                self.assertIn(b"codec_name=" + codec, probe)
                if fmt == "mp4":
                    self.assertIn(b"width=320", probe)
                    self.assertIn(b"height=240", probe)

    async def test_invalid_credentials(self):
        self.assertEqual((await self.convert("mp3", key="wrong")).status_code, 401)

    async def test_invalid_media_and_mime(self):
        self.assertEqual((await self.convert("mp4", payload=b"not a video")).status_code, 422)
        self.assertEqual((await self.convert("mp4", mime="text/plain")).status_code, 415)

    async def test_oversize(self):
        payload = io.BytesIO(b"0" * (main.MAX_SIZE + 1))
        self.assertEqual((await self.convert("mp4", payload=payload)).status_code, 413)

    async def test_url_unavailable(self):
        result = await self.client.post("/convert-url", headers={"x-api-key": "local-test-key"})
        self.assertEqual(result.status_code, 503)


if __name__ == "__main__":
    unittest.main()
