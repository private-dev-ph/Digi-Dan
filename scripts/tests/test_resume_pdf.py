import io
import subprocess
import types
import unittest
from unittest import mock

from pypdf import PdfWriter
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
from api.knowledge import resume_pdf as rp

TEXT = "Daniel Zachary has extensive experience building secure software and leading teams across technology projects."


def make_pdf(text=TEXT, count=1, blank_last=False):
    writer = PdfWriter()
    for index in range(count):
        page = writer.add_blank_page(width=300, height=300)
        if text is not None and not (blank_last and index == count - 1):
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 20 250 Td (" + text.encode() + b") Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
                DictionaryObject({NameObject("/F1"): font})})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def tsv(confidence=90):
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    rows = [f"5\t1\t1\t1\t{1 if i < 5 else 2}\t{i}\t0\t0\t1\t1\t{confidence}\t{word}"
            for i, word in enumerate(TEXT.split())]
    return header + "\n" + "\n".join(rows)


class PdfExtractionTests(unittest.TestCase):
    def test_native_text(self):
        result = rp._extract(make_pdf(), "unused")
        self.assertEqual(result["pages"][0]["method"], "text")
        self.assertEqual(result["pages"][0]["text"], TEXT)
        self.assertIsNone(result["extractor"]["ocr_engine"])

    def test_letter_spaced_text_requires_ocr(self):
        spaced = " ".join(TEXT)
        self.assertFalse(rp._adequate(spaced))
        with mock.patch.object(rp, "_ocr_page", return_value=(TEXT, 95.0)) as ocr, \
             mock.patch.object(rp, "_tesseract_version", return_value="test OCR"), \
             mock.patch.object(rp, "_pdfium_version", return_value="test renderer"):
            result = rp._extract(make_pdf(spaced), "tesseract")
        ocr.assert_called_once()
        self.assertEqual(result["pages"][0]["method"], "ocr")
        self.assertEqual(result["pages"][0]["text"], TEXT)

    def test_public_spawn_success(self):
        self.assertEqual(rp.extract_pdf(make_pdf())["page_count"], 1)

    def test_public_spawn_error_propagation(self):
        with self.assertRaises(rp.ResumeExtractionError) as error:
            rp.extract_pdf(b"%PDF-garbage")
        self.assertEqual(error.exception.code, "invalid_pdf")

    def test_invalid_size_header_and_structure(self):
        for data, code in [(b"bad", "invalid_pdf"), (b"%PDF-garbage", "invalid_pdf"),
                           (b"%PDF-" + b"x" * rp.MAX_BYTES, "input_size")]:
            with self.subTest(code=code), self.assertRaises(rp.ResumeExtractionError) as error:
                rp._extract(data, "unused")
            self.assertEqual(error.exception.code, code)

    def test_encryption_and_page_limit(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("secret")
        output = io.BytesIO()
        writer.write(output)
        for data, code in [(output.getvalue(), "encrypted_pdf"), (make_pdf(count=11), "page_count")]:
            with self.subTest(code=code), self.assertRaises(rp.ResumeExtractionError) as error:
                rp._extract(data, "unused")
            self.assertEqual(error.exception.code, code)

    def test_mixed_document_does_not_silently_drop_unreadable_page(self):
        with mock.patch.object(rp, "_ocr_page", side_effect=rp.ResumeExtractionError("ocr_unavailable", "missing")):
            with self.assertRaises(rp.ResumeExtractionError) as error:
                rp._extract(make_pdf(count=2, blank_last=True), "unused")
        self.assertEqual(error.exception.code, "ocr_unavailable")

    def test_confidence_and_line_boundaries(self):
        text, mean = rp._tsv_text(tsv())
        self.assertEqual(mean, 90)
        self.assertIn("\n", text)
        self.assertEqual(" ".join(text.split()), TEXT)
        for value in (40, "NaN", "Infinity", "oops", 101, -1):
            with self.subTest(confidence=value), self.assertRaises(rp.ResumeExtractionError):
                rp._tsv_text(tsv(value))

    def test_ocr_timeout_closes_resources(self):
        pdf, page, bitmap, image = (mock.MagicMock() for _ in range(4))
        pdf.__getitem__.return_value = page
        page.get_size.return_value = (100, 100)
        page.render.return_value = bitmap
        bitmap.to_pil.return_value = image
        fake = types.SimpleNamespace(PdfDocument=lambda data: pdf)
        with mock.patch.dict("sys.modules", {"pypdfium2": fake}), \
             mock.patch.object(rp.shutil, "which", return_value="tess"), \
             mock.patch.object(rp.subprocess, "run", side_effect=subprocess.TimeoutExpired("tess", 30)):
            with self.assertRaises(rp.ResumeExtractionError) as error:
                rp._ocr_page(b"%PDF-", 0, "tesseract")
        self.assertEqual(error.exception.code, "ocr_timeout")
        for obj in (image, bitmap, page, pdf):
            obj.close.assert_called_once()

    def test_pixel_limit_checked_before_render(self):
        pdf, page = mock.MagicMock(), mock.MagicMock()
        pdf.__getitem__.return_value = page
        page.get_size.return_value = (10_000, 10_000)
        fake = types.SimpleNamespace(PdfDocument=lambda data: pdf)
        with mock.patch.dict("sys.modules", {"pypdfium2": fake}), \
             mock.patch.object(rp.shutil, "which", return_value="tess"):
            with self.assertRaises(rp.ResumeExtractionError) as error:
                rp._ocr_page(b"%PDF-", 0, "tesseract")
        self.assertEqual(error.exception.code, "image_pixel_limit")
        page.render.assert_not_called()
        pdf.close.assert_called_once()

    def test_missing_ocr_executable(self):
        with mock.patch.object(rp.shutil, "which", return_value=None):
            with self.assertRaises(rp.ResumeExtractionError) as error:
                rp._ocr_page(b"%PDF-", 0, "absent-ocr-engine")
        self.assertEqual(error.exception.code, "ocr_unavailable")

    def test_wrapper_timeout_terminates_process(self):
        receiver, sender, process, context = (mock.Mock() for _ in range(4))
        receiver.poll.return_value = False
        process.is_alive.return_value = True
        context.Pipe.return_value = (receiver, sender)
        context.Process.return_value = process
        with mock.patch.object(rp.mp, "get_context", return_value=context):
            with self.assertRaises(rp.ResumeExtractionError) as error:
                rp.extract_pdf(make_pdf())
        self.assertEqual(error.exception.code, "extraction_timeout")
        process.terminate.assert_called()
        receiver.close.assert_called_once()
        process.join.assert_called()


if __name__ == "__main__":
    unittest.main()
