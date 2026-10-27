"""Local text-layer extraction; no OCR or inferred document structure."""
from __future__ import annotations

import io
import logging
import sys
import threading

from pypdf import PdfReader

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 2000
MAX_PDF_PAGE_STREAM_BYTES = 16 * 1024 * 1024
MAX_PDF_STREAM_BYTES = 128 * 1024 * 1024
MAX_PDF_TEXT_BYTES = 20 * 1024 * 1024
MAX_PDF_FORM_DEPTH = 16
_local = threading.local()


class _PrivatePdfLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        parsing = getattr(_local, "parsing", False)
        if parsing and str(record.msg).startswith("Impossible to decode XFormObject"):
            # Upstream catches form decoding errors and continues. Do not let a
            # partly decoded page look like a complete successful extraction.
            _local.unreadable_form = True
        return not parsing


# Upstream warnings can contain source bytes. Suppress only our parser thread.
for _module in tuple(sys.modules):
    if _module == "pypdf" or _module.startswith("pypdf."):
        logging.getLogger(_module).addFilter(_PrivatePdfLogs())


class PdfTextError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def parse_pdf(raw: bytes) -> str:
    if len(raw) > MAX_PDF_BYTES:
        raise PdfTextError("pdf_limit_exceeded")
    _local.parsing = True
    _local.unreadable_form = False
    try:
        if not raw.startswith(b"%PDF-"):
            raise PdfTextError("pdf_unreadable")
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            raise PdfTextError("pdf_encrypted")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise PdfTextError("pdf_limit_exceeded")
        parts = []
        stream_bytes = text_bytes = 0
        has_text = False
        for number, page in enumerate(reader.pages, 1):
            contents = page.get_contents()
            data = contents.get_data() if contents is not None else b""
            stream_bytes += len(data)
            if len(data) > MAX_PDF_PAGE_STREAM_BYTES or stream_bytes > MAX_PDF_STREAM_BYTES:
                raise PdfTextError("pdf_limit_exceeded")
            # Form XObjects have their own content streams. Check them before
            # extract_text traverses them, including cycles and nested forms.
            pending = [(page.get("/Resources", {}), 0, frozenset())]
            while pending:
                resources, depth, ancestors = pending.pop()
                if not resources:
                    continue
                xobjects = resources.get_object().get("/XObject")
                if xobjects is None:
                    continue
                for value in xobjects.get_object().values():
                    form = value.get_object()
                    if form.get("/Subtype") != "/Form":
                        continue
                    if depth >= MAX_PDF_FORM_DEPTH or id(form) in ancestors:
                        raise PdfTextError("pdf_limit_exceeded")
                    size = len(form.get_data())
                    stream_bytes += size
                    if size > MAX_PDF_PAGE_STREAM_BYTES or stream_bytes > MAX_PDF_STREAM_BYTES:
                        raise PdfTextError("pdf_limit_exceeded")
                    if "/Resources" in form:
                        pending.append((form["/Resources"], depth + 1, ancestors | {id(form)}))
            visited_bytes = 0
            def check_text(fragment, *_):
                nonlocal visited_bytes
                visited_bytes += len(fragment.encode("utf-8"))
                if visited_bytes + text_bytes > MAX_PDF_TEXT_BYTES:
                    raise PdfTextError("pdf_limit_exceeded")
            text = page.extract_text(extraction_mode="plain", visitor_text=check_text) or ""
            if visited_bytes + text_bytes > MAX_PDF_TEXT_BYTES:
                raise PdfTextError("pdf_limit_exceeded")
            if _local.unreadable_form:
                raise PdfTextError("pdf_unreadable")
            if text.strip():
                has_text = True
            elif page.get("/Annots") or (contents is not None and any(
                operator in {b"Do", b"INLINE IMAGE", b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"sh"}
                or (operator in {b"Tj", b"TJ", b"'", b'"'} and any(operands))
                for operands, operator in contents.operations
            )):
                raise PdfTextError("pdf_page_text_unavailable")
            part = f"<!-- PDF page: {number} -->\n{text}\n"
            text_bytes += len(part.encode("utf-8")) + bool(parts)
            if text_bytes > MAX_PDF_TEXT_BYTES:
                raise PdfTextError("pdf_limit_exceeded")
            parts.append(part)
        if not has_text:
            raise PdfTextError("pdf_no_text")
        return "\n".join(parts)
    except PdfTextError:
        raise
    except Exception:
        raise PdfTextError("pdf_unreadable") from None
    finally:
        _local.parsing = False
