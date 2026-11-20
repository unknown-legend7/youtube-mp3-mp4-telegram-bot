import json
import sys
import warnings
from pathlib import Path

from PIL import Image, ImageOps
from reportlab.pdfgen import canvas

MAX_PIXELS = 8_000_000
MAX_EDGE = 6000
MAX_BATCH = 6
MAX_BYTES = 20_000_000
FORMATS = {
    "jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP",
    "bmp": "BMP", "tiff": "TIFF", "tif": "TIFF",
}
OUTPUTS = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP", "pdf": "PDF"}
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
warnings.simplefilter("error", Image.DecompressionBombWarning)


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ImageOptionError(f"{label} must be between {low} and {high}.")
    return value


def check_dimensions(width, height):
    if max(width, height) > MAX_EDGE or width * height > MAX_PIXELS:
        raise ImageOptionError("Image exceeds 8 megapixels or 6000 pixels on one side.")


def load_image(path, extension):
    with Image.open(path, formats=list(set(FORMATS.values()))) as source:
        if source.format != FORMATS[extension]:
            raise ImageOptionError("Image content does not match its filename extension.")
        check_dimensions(*source.size)
        # A second frame is enough to reject animated images and multipage TIFFs.
        try:
            source.seek(1)
        except EOFError:
            source.seek(0)
        else:
            raise ImageOptionError("Animated images and multipage TIFFs are unsupported. Send a single frame.")
        image = ImageOps.exif_transpose(source)
        return image


def flatten(image, background):
    if background not in {"white", "black"}:
        raise ImageOptionError("Choose a white or black background.")
    rgba = image.convert("RGBA")
    base = Image.new("RGB", rgba.size, background)
    base.paste(rgba, mask=rgba.getchannel("A"))
    return base


def resize(image, options):
    mode = options.get("mode")
    width, height = image.size
    if mode == "width":
        new_width = integer(options.get("width"), 1, MAX_EDGE, "Width")
        size = (new_width, max(1, round(height * new_width / width)))
    elif mode == "height":
        new_height = integer(options.get("height"), 1, MAX_EDGE, "Height")
        size = (max(1, round(width * new_height / height)), new_height)
    elif mode == "percent":
        scale = integer(options.get("percent"), 1, 300, "Percentage") / 100
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
    elif mode == "exact":
        size = (
            integer(options.get("width"), 1, MAX_EDGE, "Width"),
            integer(options.get("height"), 1, MAX_EDGE, "Height"),
        )
    elif mode == "fit":
        bounds = (
            integer(options.get("width"), 1, MAX_EDGE, "Width"),
            integer(options.get("height"), 1, MAX_EDGE, "Height"),
        )
        scale = min(bounds[0] / width, bounds[1] / height, 1)
        size = (max(1, round(width * scale)), max(1, round(height * scale)))
    else:
        raise ImageOptionError("Choose width, height, percentage, exact dimensions, or fit.")
    if options.get("upscale") is not True and (size[0] > width or size[1] > height):
        raise ImageOptionError("This resize would enlarge the image. Explicitly allow upscaling to continue.")
    check_dimensions(*size)
    return image.resize(size, Image.Resampling.LANCZOS)


def write_pdf(inputs, output, options):
    page = options.get("page", "fit")
    orientation = options.get("orientation", "auto")
    if page not in {"fit", "a4", "letter"} or orientation not in {"auto", "portrait", "landscape"}:
        raise ImageOptionError("Choose A4, Letter, or fit and auto, portrait, or landscape orientation.")
    margin = integer(options.get("margin", 0), 0, 72, "Margin")
    document = canvas.Canvas(str(output), pageCompression=1)
    for index, (path, extension) in enumerate(inputs):
        with load_image(path, extension) as image, flatten(image, "white") as rgb:
            rgb.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            width, height = rgb.size
            if page == "fit":
                page_w, page_h = width * 0.75 + 2 * margin, height * 0.75 + 2 * margin
            else:
                page_w, page_h = (595.28, 841.89) if page == "a4" else (612, 792)
            landscape = orientation == "landscape" or (orientation == "auto" and width > height)
            page_w, page_h = sorted((page_w, page_h), reverse=landscape)
            scale = min((page_w - 2 * margin) / width, (page_h - 2 * margin) / height)
            draw_w, draw_h = width * scale, height * scale
            jpeg = output.parent / f"page-{index}.jpg"
            rgb.save(jpeg, "JPEG", quality=88)
            document.setPageSize((page_w, page_h))
            document.drawImage(str(jpeg), (page_w - draw_w) / 2, (page_h - draw_h) / 2, draw_w, draw_h)
            document.showPage()
            jpeg.unlink()
    document.save()


def convert(inputs, output, options, ext):
    if ext == "pdf":
        write_pdf(inputs, output, options)
        return None
    path, extension = inputs[0]
    with load_image(path, extension) as original:
        profile = original.info.get("icc_profile")
        image = original
        if options.get("mode"):
            image = resize(original, options)
        if ext == "jpg":
            rendered = flatten(image, options.get("background", "white"))
        else:
            rendered = image.convert("RGBA" if image.has_transparency_data else
                                     "L" if image.mode in {"1", "L"} else "RGB")
        rendered.info.clear()
        save_args = {}
        if (isinstance(profile, bytes) and len(profile) <= 500_000
                and ((original.mode in {"RGB", "RGBA", "P"} and rendered.mode in {"RGB", "RGBA"})
                     or original.mode == rendered.mode == "L")):
            save_args["icc_profile"] = profile
        if ext == "png":
            save_args["optimize"] = options.get("compress") is True
        else:
            save_args["quality"] = integer(options.get("quality", 82), 35, 100, "Quality")
        target = options.get("target_kb")
        if target is not None:
            if ext == "png":
                raise ImageOptionError("Target size requires JPEG or WebP.")
            target = integer(target, 1, 20_000, "Target KB") * 1000
        rendered.save(output, OUTPUTS[ext], **save_args)
        if target:
            quality = save_args["quality"]
            while output.stat().st_size > target and quality > 35:
                quality = max(35, quality - 10)
                rendered.save(output, OUTPUTS[ext], **{**save_args, "quality": quality})
        return target is None or output.stat().st_size <= target


def main():
    try:
        if sys.platform.startswith("linux"):
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES * 2,) * 2)
        manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        output = Path(manifest["output"])
        target_met = convert(manifest["inputs"], output, manifest["options"], manifest["format"])
        print(json.dumps({"target_met": target_met}))
    except ValueError as exc:
        # Only the converter's own validation messages may leave the worker.
        print(json.dumps({"error": str(exc) if type(exc) is ImageOptionError else
                          "Invalid image or conversion options."}))
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        print(json.dumps({"error": "Invalid, corrupted, or oversized image."}))


class ImageOptionError(ValueError):
    pass


if __name__ == "__main__":
    main()
