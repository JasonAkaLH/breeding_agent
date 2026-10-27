import logging
import unittest
from unittest.mock import patch

from src.capabilities.knowledge.pdf import PdfTextError, parse_pdf
from tests.capabilities.knowledge.pdf_fixtures import text_pdf, unreadable_page_pdf


class PdfTest(unittest.TestCase):
    def test_scan_mixed_and_encrypted_fail_closed(self):
        for options, code in [({}, "pdf_page_text_unavailable"), ({"mixed": True}, "pdf_page_text_unavailable"), ({"encrypted": True}, "pdf_encrypted")]:
            with self.subTest(options=options), self.assertRaises(PdfTextError) as error:
                parse_pdf(unreadable_page_pdf(**options))
            self.assertEqual(error.exception.code, code)

    def test_page_order_blank_page_and_no_invented_title(self):
        result = parse_pdf(text_pdf("FIRST evidence", None, "", "LAST evidence"))
        self.assertTrue(result.startswith("<!-- PDF page: 1 -->\n"))
        self.assertIn("<!-- PDF page: 2 -->", result)
        self.assertLess(result.index("FIRST"), result.index("LAST"))
        self.assertNotIn("# ", result)

    def test_upstream_warning_privacy_and_partial_form_failure(self):
        from io import BytesIO
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(text_pdf("Private text")))
        def warning(*args, **kwargs):
            logging.getLogger("pypdf._page").warning("Impossible to decode XFormObject %(operand)s: %(exception)s", {"operand": "PRIVATE", "exception": "PRIVATE"})
            return reader
        with patch("src.capabilities.knowledge.pdf.PdfReader", side_effect=warning), self.assertNoLogs("pypdf", level="WARNING"), self.assertRaises(PdfTextError) as error:
            parse_pdf(text_pdf("Private text"))
        self.assertEqual(error.exception.code, "pdf_unreadable")
        with self.assertLogs("pypdf._page", level="WARNING"):
            logging.getLogger("pypdf._page").warning("unrelated caller warning")

    def test_invalid_empty_and_limits_fail_without_truncation(self):
        for raw, code in [(b"%PDF-broken", "pdf_unreadable"), (text_pdf(None), "pdf_no_text")]:
            with self.subTest(code=code), self.assertRaises(PdfTextError) as error:
                parse_pdf(raw)
            self.assertEqual(error.exception.code, code)
        for limit, value in [("MAX_PDF_PAGES", 1), ("MAX_PDF_TEXT_BYTES", 1), ("MAX_PDF_PAGE_STREAM_BYTES", 1), ("MAX_PDF_STREAM_BYTES", 1)]:
            with self.subTest(limit=limit), patch(f"src.capabilities.knowledge.pdf.{limit}", value), self.assertRaises(PdfTextError) as error:
                parse_pdf(text_pdf("first", "last"))
            self.assertEqual(error.exception.code, "pdf_limit_exceeded")

    def test_exact_limits_are_inclusive(self):
        raw = text_pdf("Evidence")
        text = parse_pdf(raw)
        with patch("src.capabilities.knowledge.pdf.MAX_PDF_BYTES", len(raw)), patch("src.capabilities.knowledge.pdf.MAX_PDF_TEXT_BYTES", len(text.encode())):
            self.assertEqual(parse_pdf(raw), text)
        with patch("src.capabilities.knowledge.pdf.MAX_PDF_BYTES", len(raw) - 1), self.assertRaises(PdfTextError) as error:
            parse_pdf(raw)
        self.assertEqual(error.exception.code, "pdf_limit_exceeded")
