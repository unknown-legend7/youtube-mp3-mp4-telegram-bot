import os
import subprocess
import stat
from functools import lru_cache
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree
from zipfile import ZipFile

from pdf_tools import JobError, MAX_PAGES, pdf_reader

BWRAP = "/usr/bin/bwrap"
OFFICE = "/usr/bin/libreoffice"
GHOSTSCRIPT = "/usr/bin/gs"
VERAPDF = "/opt/verapdf/verapdf"
ICC = "/usr/share/color/icc/ghostscript/srgb.icc"
OFFICE_TYPES = {
    "docx": ("word/document.xml", "writer_pdf_Export"),
    "pptx": ("ppt/presentation.xml", "impress_pdf_Export"),
    "xlsx": ("xl/workbook.xml", "calc_pdf_Export"),
}


def sandbox(directory):
    command = [
        BWRAP, "--unshare-all", "--die-with-parent", "--new-session",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
        "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", "/var", "--dir", "/var/opt/verapdf/logs",
        "--ro-bind-try", "/etc/fonts", "/etc/fonts",
        "--ro-bind-try", "/etc/libreoffice/registry", "/etc/libreoffice/registry",
        "--ro-bind-try", "/etc/java-17-openjdk", "/etc/java-17-openjdk",
        "--ro-bind-try", "/etc/java-21-openjdk", "/etc/java-21-openjdk",
        "--ro-bind-try", "/opt/verapdf", "/opt/verapdf",
        "--bind", str(directory), "/job", "--chdir", "/job", "--clearenv",
        "--setenv", "PATH", "/usr/lib/jvm/default-java/bin:/usr/bin", "--setenv", "LANG", "C.UTF-8",
        "--setenv", "USER", "converter", "--setenv", "HOME", "/job",
        "--setenv", "TMPDIR", "/tmp", "--setenv", "JAVA_HOME", "/usr/lib/jvm/default-java",
        "--setenv", "JAVA_TOOL_OPTIONS",
        "-Xms16m -Xmx128m -XX:MaxMetaspaceSize=96m -XX:CompressedClassSpaceSize=64m -XX:ReservedCodeCacheSize=32m -XX:ActiveProcessorCount=1",
        "--",
    ]
    return command


@lru_cache(maxsize=1)
def native_features():
    unavailable = {"office": False, "pdfa": False}
    if os.getenv("CONVERTER_ENABLE_NATIVE_DOCUMENTS") != "1":
        return {**unavailable, "native_status": "activation_disabled"}

    office = Path(OFFICE).is_file()
    pdfa = all(Path(path).is_file() for path in (GHOSTSCRIPT, VERAPDF, ICC))
    if not Path(BWRAP).is_file() or not (office or pdfa):
        return {**unavailable, "native_status": "packages_missing"}

    import tempfile

    try:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                sandbox(directory) + ["/usr/bin/true"], stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, text=True, errors="replace", timeout=4,
            )
    except (TimeoutError, subprocess.TimeoutExpired):
        return {**unavailable, "native_status": "sandbox_timeout"}
    except OSError:
        return {**unavailable, "native_status": "sandbox_failed"}
    if result.returncode:
        stderr = result.stderr.lower()
        denied = any(marker in stderr for marker in (
            "operation not permitted", "permission denied", "no permissions",
        ))
        return {**unavailable, "native_status": "sandbox_denied" if denied else "sandbox_failed"}
    return {"office": office, "pdfa": pdfa,
            "native_status": "ready" if office and pdfa else "partially_ready"}


def native_command(directory, args, stdout=subprocess.DEVNULL, accepted=(0,)):
    try:
        result = subprocess.run(sandbox(directory) + args, stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=subprocess.DEVNULL, timeout=25)
    except subprocess.TimeoutExpired:
        raise JobError("Document conversion timed out.", 504) from None
    if result.returncode not in accepted:
        raise JobError("The isolated document converter could not process this file.")


def html_to_pdf(source, output):
    from weasyprint import HTML
    from weasyprint.urls import FatalURLFetchingError, URLFetcher

    class OfflineFetcher(URLFetcher):
        def fetch(self, url, headers=None):
            raise FatalURLFetchingError("External resources are blocked.")

    if source.stat().st_size > 1_000_000:
        raise JobError("HTML files may be up to 1 MB.", 413)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except UnicodeError:
        raise JobError("Save your HTML file with UTF-8 encoding.") from None
    if "\x00" in text or "<" not in text:
        raise JobError("Upload a valid HTML document.", 415)
    try:
        document = HTML(string=text, base_url="https://unavailable.invalid/",
                        url_fetcher=OfflineFetcher()).render()
    except FatalURLFetchingError:
        raise JobError("HTML uses external resources. Embed the text and styles directly in the file.") from None
    if not 1 <= len(document.pages) <= MAX_PAGES:
        raise JobError(f"HTML output must contain up to {MAX_PAGES} pages.")
    document.write_pdf(output)


def inspect_office_archive(source, extension):
    required, _ = OFFICE_TYPES[extension]
    with ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > 2500 or sum(info.file_size for info in entries) > 64_000_000:
            raise JobError("Office archive expands beyond the safe processing limit.", 413)
        names = {info.filename for info in entries}
        if len(names) != len(entries) or not {required, "[Content_Types].xml", "_rels/.rels"} <= names:
            raise JobError("Office content does not match its filename extension.", 415)
        types = archive.getinfo("[Content_Types].xml")
        if types.file_size > 512_000 or b"macroenabled" in archive.read(types).lower():
            raise JobError("Macro-enabled Office documents are unsupported.")
        for info in entries:
            name = info.filename.lower()
            parts = PurePosixPath(name).parts
            if (name.startswith("/") or "\\" in name or ":" in name or ".." in parts or info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16)
                    or any(term in name for term in ("vbaproject", "/activex/", "/embeddings/", "/externallinks/"))):
                raise JobError("Encrypted, embedded, macro-enabled, or unsafe office content is unsupported.")
            if name.endswith(".rels"):
                if info.file_size > 512_000:
                    raise JobError("Office relationship data is too large.", 413)
                contents = archive.read(info)
                if b"\x00" in contents or b"<!DOCTYPE" in contents.upper() or b"<!ENTITY" in contents.upper():
                    raise JobError("Office XML declarations are unsupported.")
                for link in ElementTree.fromstring(contents):
                    if link.get("TargetMode", "").lower() == "external":
                        raise JobError("Remove external links and linked resources before converting.")


def office_to_pdf(source, directory):
    extension = source.suffix[1:]
    inspect_office_archive(source, extension)
    _, export_filter = OFFICE_TYPES[extension]
    native_command(directory, [
        OFFICE, "-env:UserInstallation=file:///job/profile", "--headless", "--norestore",
        "--nodefault", "--nolockcheck", "--nologo", "--convert-to", "pdf:" + export_filter,
        "--outdir", "/job", "/job/" + source.name,
    ])
    generated = source.with_suffix(".pdf")
    if generated.is_symlink() or not generated.is_file():
        raise JobError("LibreOffice did not produce a PDF.")
    pdf_reader(generated)
    return {"file": generated.name, "note": "Fonts, formulas, animations and complex formatting may differ."}


def validate_pdfa_report(path):
    if path.stat().st_size > 2_000_000:
        raise JobError("The PDF/A validation report exceeded its limit.")
    root = ElementTree.parse(path).getroot()
    reports = [item for item in root.iter() if item.tag.split("}")[-1] == "validationReport"]
    if (len(reports) != 1 or reports[0].get("isCompliant") != "true"
            or reports[0].get("profileName", "").casefold() != "pdf/a-2b validation profile"):
        raise JobError("Output failed veraPDF PDF/A-2b validation.")


def pdf_to_pdfa(source, directory):
    pdf_reader(source)
    definition = directory / "intent.ps"
    definition.write_text(
        "%!PS\n"
        "[ /Title (Archived PDF) /DOCINFO pdfmark\n"
        "[/_objdef {icc_PDFA} /type /stream /OBJ pdfmark\n"
        "[{icc_PDFA} << /N 3 >> /PUT pdfmark\n"
        f"[{{icc_PDFA}} ({ICC}) (r) file /PUT pdfmark\n"
        "[/_objdef {OutputIntent_PDFA} /type /dict /OBJ pdfmark\n"
        "[{OutputIntent_PDFA} << /Type /OutputIntent /S /GTS_PDFA1\n"
        " /DestOutputProfile {icc_PDFA} /OutputConditionIdentifier (sRGB) >> /PUT pdfmark\n"
        "[{Catalog} << /OutputIntents [ {OutputIntent_PDFA} ] >> /PUT pdfmark\n",
        encoding="ascii",
    )
    target = directory / "archival.pdf"
    native_command(directory, [
        GHOSTSCRIPT, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dPDFA=2",
        "-dPDFACompatibilityPolicy=2", "-sDEVICE=pdfwrite", "-sColorConversionStrategy=RGB",
        "--permit-file-read=" + ICC, "-sOutputFile=/job/" + target.name,
        "/job/" + definition.name, "/job/" + source.name,
    ])
    if target.is_symlink() or not target.is_file():
        raise JobError("Ghostscript did not produce an archival PDF.")
    report = directory / "validation.xml"
    with report.open("wb") as stream:
        native_command(directory, [VERAPDF, "--format", "xml", "--flavour", "2b", "/job/" + target.name],
                       stdout=stream, accepted=(0, 1))
    validate_pdfa_report(report)
    return {"file": target.name, "note": "PDF/A-2b verified by veraPDF."}
