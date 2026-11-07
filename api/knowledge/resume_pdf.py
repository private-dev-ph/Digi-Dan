"""Bounded local PDF text and OCR extraction for resume review.

Extracted text is raw evidence only; it must be reviewed before becoming an
approved fact. This module performs no network or storage operations.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import shutil
import subprocess
import tempfile
import math
from typing import Any

MAX_BYTES = 5 * 1024 * 1024
MAX_PAGES = 10
MAX_PAGE_CHARS = 30_000
MAX_TOTAL_CHARS = 100_000
POLICY_VERSION = "2"


class ResumeExtractionError(ValueError):
    """An extraction failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _error(code: str, message: str) -> ResumeExtractionError:
    return ResumeExtractionError(code, message)


def _tsv_text(tsv: str) -> tuple[str, float]:
    import csv
    from io import StringIO

    rows = csv.DictReader(StringIO(tsv), delimiter="\t")
    groups: dict[tuple[str, str, str], list[str]] = {}
    confidences: list[float] = []
    low = 0
    for row in rows:
        word = (row.get("text") or "").strip()
        if not word:
            continue
        try:
            confidence = float(row.get("conf", "-1"))
        except ValueError as exc:
            raise _error("ocr_unreadable", "OCR returned malformed confidence") from exc
        if not math.isfinite(confidence) or not 0 <= confidence <= 100:
            raise _error("ocr_unreadable", "OCR returned invalid confidence")
        confidences.append(confidence)
        low += confidence < 50
        key = (row.get("block_num", ""), row.get("par_num", ""), row.get("line_num", ""))
        groups.setdefault(key, []).append(word)
    if not confidences:
        raise _error("ocr_unreadable", "OCR produced no recognized words")
    mean = sum(confidences) / len(confidences)
    if mean < 75 or low / len(confidences) > 0.25:
        raise _error("ocr_low_confidence", "OCR confidence is below policy threshold")
    text = "\n".join(" ".join(words) for words in groups.values())
    return text, mean


def _adequate(text: str) -> bool:
    import re

    tokens = re.findall(r"\b\w+\b", text)
    meaningful = sum(len(token) >= 2 for token in tokens)
    single_ratio = sum(len(token) == 1 for token in tokens) / max(1, len(tokens))
    return (sum(ch.isalnum() for ch in text) >= 40 and meaningful >= 8
            and single_ratio <= 0.35 and "\ufffd" not in text)


def _extract(data: bytes, tesseract_cmd: str) -> dict[str, Any]:
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise _error("input_size", "PDF must be bytes and no larger than 5 MiB")
    if not data.startswith(b"%PDF-"):
        raise _error("invalid_pdf", "Input does not have a PDF header")
    try:
        from pypdf import PdfReader
        import pypdf
    except Exception as exc:
        raise _error("dependency_missing", "PDF parser is unavailable") from exc
    try:
        reader = PdfReader(__import__("io").BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise _error("encrypted_pdf", "Encrypted PDFs are not supported")
        count = len(reader.pages)
        if not count or count > MAX_PAGES:
            raise _error("page_count", "PDF must contain between 1 and 10 pages")
    except ResumeExtractionError:
        raise
    except Exception as exc:
        raise _error("invalid_pdf", "PDF is corrupt or unreadable") from exc

    pages = []
    total = 0
    used_ocr = False
    for index, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text(extraction_mode="plain") or ""
        except Exception:
            text = ""
        text = text.strip()
        method, confidence = "text", None
        if not _adequate(text):
            used_ocr = True
            text, confidence = _ocr_page(data, index - 1, tesseract_cmd)
            method = "ocr"
        if not _adequate(text):
            raise _error("page_unreadable", f"Page {index} has insufficient readable text")
        if len(text) > MAX_PAGE_CHARS:
            raise _error("page_text_limit", f"Page {index} exceeds text limit")
        total += len(text)
        if total > MAX_TOTAL_CHARS:
            raise _error("document_text_limit", "PDF exceeds total text limit")
        pages.append({"page_number": index, "text": text, "method": method, "ocr_mean_confidence": confidence})
    return {
        "page_count": count,
        "pages": pages,
        "extractor": {"policy_version": POLICY_VERSION, "pypdf_version": pypdf.__version__,
                      "ocr_engine": _tesseract_version(tesseract_cmd) if used_ocr else None,
                      "pdfium_version": _pdfium_version() if used_ocr else None},
    }


def _ocr_page(data: bytes, page_index: int, tesseract_cmd: str) -> tuple[str, float]:
    try:
        executable = shutil.which(tesseract_cmd) if not os.path.isabs(tesseract_cmd) else (tesseract_cmd if os.path.isfile(tesseract_cmd) else None)
        if not executable:
            raise _error("ocr_unavailable", "Tesseract is not available")
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
        page = pdf[page_index]
        width, height = page.get_size()
        if not all(math.isfinite(value) and value > 0 for value in (width, height)) or math.ceil(width * 3) * math.ceil(height * 3) > 20_000_000:
            raise _error("image_pixel_limit", "Rendered page exceeds 20 million pixels")
        bitmap = page.render(scale=3.0)
        image = bitmap.to_pil()
        with tempfile.TemporaryDirectory(prefix="resume-ocr-") as directory:
            path = os.path.join(directory, "page.png")
            image.save(path, format="PNG")
            try:
                result = subprocess.run([executable, path, "stdout", "-l", "eng", "--psm", "3", "tsv"],
                                        capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=30, check=False)
            except subprocess.TimeoutExpired as exc:
                raise _error("ocr_timeout", "Tesseract exceeded its page timeout") from exc
            if result.returncode:
                raise _error("ocr_failed", "Tesseract failed to recognize the page")
            return _tsv_text(result.stdout)
    except ResumeExtractionError:
        raise
    except Exception as exc:
        raise _error("ocr_failed", "Could not render or recognize PDF page") from exc
    finally:
        for name in ("image", "bitmap", "page", "pdf"):
            obj = locals().get(name)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass


def _worker(data: bytes, command: str, connection: Any) -> None:
    try:
        connection.send((True, _extract(data, command)))
    except ResumeExtractionError as exc:
        connection.send((False, exc.code, str(exc)))
    except BaseException:
        connection.send((False, "worker_failed", "PDF extraction worker failed"))
    finally:
        connection.close()


def _tesseract_version(command: str) -> str:
    executable = shutil.which(command) if not os.path.isabs(command) else (command if os.path.isfile(command) else None)
    if not executable:
        raise _error("ocr_unavailable", "Tesseract is not available")
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                encoding="utf-8", errors="strict", timeout=5, check=False)
    except subprocess.TimeoutExpired as exc:
        raise _error("ocr_timeout", "Tesseract version check timed out") from exc
    if result.returncode:
        raise _error("ocr_unavailable", "Tesseract version check failed")
    lines = (result.stdout or result.stderr).splitlines()
    if not lines:
        raise _error("ocr_unavailable", "Tesseract version check returned no version")
    return lines[0][:200]


def _pdfium_version() -> str:
    import pypdfium2
    return str(pypdfium2.PYPDFIUM_INFO)


def extract_pdf(data: bytes, *, tesseract_cmd: str = "tesseract") -> dict:
    """Extract text in a spawned process, bounded by a 120-second deadline."""
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise _error("input_size", "PDF must be bytes and no larger than 5 MiB")
    if not data.startswith(b"%PDF-"):
        raise _error("invalid_pdf", "Input does not have a PDF header")
    context = mp.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(data, tesseract_cmd, sender), daemon=True)
    started = False
    try:
        process.start()
        started = True
        sender.close()
        if not receiver.poll(120):
            process.terminate()
            raise _error("extraction_timeout", "PDF extraction exceeded 120 seconds")
        message = receiver.recv()
        process.join(timeout=2)
        if message[0]:
            return message[1]
        raise _error(message[1], message[2])
    except EOFError as exc:
        raise _error("worker_failed", "PDF extraction worker exited unexpectedly") from exc
    finally:
        sender.close()
        receiver.close()
        if started:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)
