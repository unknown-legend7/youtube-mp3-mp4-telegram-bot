import json
import sys
from pathlib import Path
from zipfile import BadZipFile

from pypdf.errors import PdfReadError

from document_native import html_to_pdf, office_to_pdf, pdf_to_pdfa
from pdf_office import pdf_to_office
from pdf_tools import JobError, pdf_operation


def main():
    try:
        # The manifest travels over stdin. PDF passwords never reach disk or argv.
        job = json.load(sys.stdin)
        action = job["action"]
        if sys.platform.startswith("linux"):
            import resource

            address_limit = (2304 if action in {"office", "pdfa"} else 400) * 1024**2
            resource.setrlimit(resource.RLIMIT_AS, (address_limit, address_limit))
            resource.setrlimit(resource.RLIMIT_CPU, (29, 29))
            resource.setrlimit(resource.RLIMIT_FSIZE, (40_000_000, 40_000_000))

        inputs = [Path(path) for path in job["inputs"]]
        directory = Path.cwd()
        if action == "html":
            html_to_pdf(inputs[0], directory / "converted.pdf")
            result = {"file": "converted.pdf", "note": "Rendered with scripts and external resources disabled."}
        elif action == "office":
            result = office_to_pdf(inputs[0], directory)
        elif action == "pdfa":
            result = pdf_to_pdfa(inputs[0], directory)
        elif action in {"docx", "xlsx", "pptx"}:
            result = pdf_to_office(inputs[0], directory, action, job["settings"])
        else:
            result = pdf_operation(inputs, directory, action, job["settings"], job.get("password"))
        print(json.dumps(result))
    except JobError as exc:
        print(json.dumps({"error": str(exc), "status": exc.status}))
    except (OSError, ValueError, PdfReadError, BadZipFile, MemoryError):
        print(json.dumps({"error": "The document is invalid, unsupported, or exceeds processing limits.", "status": 422}))


if __name__ == "__main__":
    main()
