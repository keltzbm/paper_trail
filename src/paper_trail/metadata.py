"""Bibliographic metadata lookup for a local PDF, via the public Crossref API.

Resolution order (see ``get_metadata``): filename ISBN/DOI fast path, embedded
PDF text, OCR fallback for scanned pages, then a fuzzy Crossref title search.
"""

import contextlib
import os
import re
import sys
import threading
from pathlib import Path

from paper_trail.http_client import get_http_session

# PDF-page-to-image rasterization, needed by either OCR backend below.
try:
    from pdf2image import convert_from_path

    HAS_PDF2IMAGE = True
except ImportError:
    HAS_PDF2IMAGE = False

# Tesseract: CPU-only, works on macOS/Windows/Linux.
try:
    import pytesseract

    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

# Apple's Vision framework via ocrmac: macOS only, runs on the GPU/Neural
# Engine instead of the CPU. Noticeably faster (and often more accurate)
# on Apple Silicon, but naturally unavailable on Windows/Linux.
try:
    from ocrmac import ocrmac

    HAS_VISION = True
except ImportError:
    HAS_VISION = False

HAS_OCR = HAS_PDF2IMAGE and (HAS_TESSERACT or HAS_VISION)

DOI_REGEX = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
ISBN_REGEX = re.compile(r"978-\d{1,5}-\d{1,7}-\d{1,7}-[\dX]")

# Pre-~2007 Springer titles used a compact code as the literal DOI suffix
# instead of an ISBN (e.g. "b138671" == 10.1007/b138671, "BFb0063954" ==
# 10.1007/BFb0063954). A bare filename matching this shows up as an
# unresolvable "random" name, but it's actually a directly-usable DOI.
LEGACY_SPRINGER_REGEX = re.compile(r"^(?:BF[bmc]|[bm])\d{4,8}$")

CROSSREF_WORKS_URL = "https://api.crossref.org/works"

# fetch_crossref/search_crossref_by_title can each be called several times
# per file (ISBN fast path, legacy-DOI fast path, filename-DOI fast path,
# post-OCR, final title search). A fresh requests.Session() per call throws
# away connection keep-alive, paying a new TCP+TLS handshake to Crossref
# every time. One shared, lazily-created Session (safe across threads for
# our read-only GET usage) lets urllib3 actually reuse connections.
_session = None
_session_lock = threading.Lock()


def _get_session():
    """Returns a process-wide requests.Session, creating it on first use."""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                _session = get_http_session()
    return _session


# File descriptors 1/2 (stdout/stderr) are process-global, not per-thread.
# Without this lock, two worker threads calling _suppressed_output()
# concurrently can interleave their dup2()/close() calls and permanently
# corrupt the real stdout/stderr for the whole process (one thread's
# "restore" can stomp on another thread's still-active "redirect", and an
# os.close() can end up closing a descriptor that's since been reused for
# something else entirely). Serializing this brief section fixes it.
_stdio_lock = threading.Lock()


@contextlib.contextmanager
def _suppressed_output():
    """Silences noisy stdout/stderr from underlying libs (e.g. pypdf/pdf2doi)."""
    with _stdio_lock:
        null_fd = os.open(os.devnull, os.O_WRONLY)
        old_stdout = os.dup(1)
        old_stderr = os.dup(2)
        try:
            os.dup2(null_fd, 1)
            os.dup2(null_fd, 2)
            yield
        finally:
            os.dup2(old_stdout, 1)
            os.dup2(old_stderr, 2)
            os.close(old_stdout)
            os.close(old_stderr)
            os.close(null_fd)


def _parse_work_item(item: dict) -> dict:
    """Normalizes a Crossref `work` item into the metadata dict shape we use."""
    date_parts = (
        item.get("published-print", {}).get("date-parts")
        or item.get("published-online", {}).get("date-parts")
        or item.get("created", {}).get("date-parts")
    )
    year = date_parts[0][0] if date_parts and date_parts[0] else None

    titles = item.get("title", [])
    title = titles[0] if titles else None

    authors = item.get("author", [])
    author = authors[0].get("family", "") if authors else None

    return {
        "doi": item.get("DOI"),
        "title": title,
        "author": author,
        "year": year,
    }


def fetch_crossref(doi: str) -> dict | None:
    """Queries the Crossref API using a DOI to retrieve metadata."""
    if not doi:
        return None

    clean_doi = doi.rstrip(".;),]")
    session = _get_session()
    url = f"{CROSSREF_WORKS_URL}/{clean_doi}"

    try:
        res = session.get(url, timeout=3)
        if res.status_code == 200:
            item = res.json().get("message", {})
            meta = _parse_work_item(item)
            meta["doi"] = clean_doi
            return meta
    except Exception:
        pass

    return None


def search_crossref_by_title(query_text: str) -> dict | None:
    """Performs a fuzzy metadata search on Crossref using extracted title/text."""
    if not query_text or len(query_text.strip()) < 5:
        return None

    session = _get_session()
    params = {
        "query.bibliographic": query_text[:200],  # Limit length for query
        "rows": 1,
    }

    try:
        res = session.get(CROSSREF_WORKS_URL, params=params, timeout=3)
        if res.status_code == 200:
            items = res.json().get("message", {}).get("items", [])
            if items:
                return _parse_work_item(items[0])
    except Exception:
        pass

    return None


def iter_pdf_text_doi_candidates(pdf_path: Path, max_pages: int = 3):
    """Yields candidate DOIs found in a PDF's embedded text, page by page.

    A page can mention more than one DOI-shaped string (the paper's own,
    plus ones it cites), and the first one found isn't necessarily the
    right one. This yields every candidate in reading order — page 1's
    matches first, then page 2's, etc. — so the caller (``get_metadata``)
    can validate each against Crossref and only move on when one fails,
    rather than the whole method giving up after a single wrong guess.
    """
    try:
        import pypdf

        with _suppressed_output():
            reader = pypdf.PdfReader(str(pdf_path))
            for page in reader.pages[:max_pages]:
                text = page.extract_text() or ""
                for match in DOI_REGEX.finditer(text):
                    yield match.group(0)
    except Exception:
        return


def _ocr_via_vision(image, min_confidence: float = 0.3) -> str:
    """Runs OCR via Apple's Vision framework (GPU/Neural Engine, macOS only).

    Vision returns a confidence score per recognized line; lines below
    ``min_confidence`` are dropped before we go looking for a DOI in the
    result. This doesn't guarantee correctness of what remains, but it
    reduces the chance that a garbled, low-confidence misread happens to
    assemble into something that merely looks DOI-shaped.
    """
    annotations = ocrmac.OCR(image).recognize()
    return " ".join(
        text for text, confidence, _box in annotations if confidence >= min_confidence
    )


def _ocr_via_tesseract(image) -> str:
    """Runs OCR via Tesseract (CPU-only, works on macOS/Windows/Linux)."""
    return pytesseract.image_to_string(image)


def iter_ocr_doi_candidates(pdf_path: Path, backend: str = "auto", max_pages: int = 3):
    """Yields candidate DOIs found via OCR, page by page (see ``iter_pdf_text_doi_candidates``).

    Args:
        pdf_path: The scanned PDF to inspect.
        backend: Which OCR engine to use:
            - "auto" (default): Apple's Vision framework on macOS when
              available (GPU/Neural Engine — faster on Apple Silicon),
              otherwise Tesseract (CPU, cross-platform).
            - "vision": force Vision. macOS only; yields nothing elsewhere.
            - "tesseract": force Tesseract, regardless of platform.
        max_pages: Rasterization and OCR happen one page at a time and stop
            as soon as the caller stops pulling from this generator (i.e.
            once a candidate validates) — pages beyond that point are never
            rasterized or OCR'd at all. This caps the worst case at
            ``max_pages`` when nothing validates.
    """
    if not HAS_PDF2IMAGE:
        return

    use_vision = backend == "vision" or (
        backend == "auto" and HAS_VISION and sys.platform == "darwin"
    )
    use_tesseract = backend == "tesseract" or (
        backend == "auto" and not use_vision and HAS_TESSERACT
    )

    if use_vision and not HAS_VISION:
        return
    if use_tesseract and not HAS_TESSERACT:
        return
    if not use_vision and not use_tesseract:
        return

    for page_num in range(1, max_pages + 1):
        try:
            images = convert_from_path(
                pdf_path, first_page=page_num, last_page=page_num, dpi=200
            )
        except Exception:
            continue
        if not images:
            continue

        try:
            text = (
                _ocr_via_vision(images[0])
                if use_vision
                else _ocr_via_tesseract(images[0])
            )
        except Exception:
            continue

        for match in DOI_REGEX.finditer(text):
            yield match.group(0).rstrip(".;),]")


def get_metadata(
    pdf_path: Path, skip_ocr: bool = False, ocr_backend: str = "auto"
) -> dict | None:
    """Resolves bibliographic metadata for a local PDF.

    Tries, in order: a Springer ISBN in the filename, DOIs found
    in the embedded PDF text, DOIs found via OCR of scanned pages (unless
    ``skip_ocr``), then a fuzzy Crossref title search on the filename. OCR
    is by far the slowest step, so ``skip_ocr=True`` trades some recall on
    scanned PDFs for a lot of speed on a large batch. ``ocr_backend``
    selects the OCR engine — see ``iter_ocr_doi_candidates`` for the options.

    The text and OCR steps don't stop at the first DOI-shaped string they
    find — a page can mention several (the paper's own, plus ones it
    cites), and a match that merely *looks* like a DOI is worthless if
    nothing online actually has it. Each candidate is validated against
    Crossref in turn, page by page, and only accepted once Crossref
    actually confirms it; if a candidate fails to validate, the next one
    (or the next page) is tried rather than giving up on the whole method.

    The returned dict always includes a ``resolved_via`` field identifying
    which method produced the match. This matters because these methods
    aren't equally trustworthy: an ISBN or DOI lifted straight from the
    filename is about as reliable as this gets, but OCR can misread a
    character into something that still happens to look like a valid DOI,
    and the final fuzzy title search can occasionally latch onto the wrong
    paper. Tagging the method lets a human spot-check just the
    lower-confidence matches later — see how ``organizer.py`` surfaces this
    in the log, the note frontmatter, and the run summary — rather than
    every organized file looking equally certain.
    """
    pdf_name = pdf_path.name

    isbn_match = ISBN_REGEX.search(pdf_name)
    if isbn_match:
        # Springer ISBNs map onto Springer DOIs under the 10.1007 prefix.
        doi = f"10.1007/{isbn_match.group(0)}"
        meta = fetch_crossref(doi)
        if meta:
            meta["resolved_via"] = "isbn"
            return meta

    legacy_match = LEGACY_SPRINGER_REGEX.match(pdf_path.stem)
    if legacy_match:
        meta = fetch_crossref(f"10.1007/{legacy_match.group(0)}")
        if meta:
            meta["resolved_via"] = "legacy_springer_doi"
            return meta

    # Deliberately no "DOI found directly in the filename" fast path here.
    # A filename is metadata someone else (or some other tool) assigned to
    # the file — it can be wrong, or reference a different work entirely —
    # unlike a DOI actually printed on the page, which is what the next two
    # steps look for instead.

    tried: set[str] = set()

    for doi in iter_pdf_text_doi_candidates(pdf_path):
        if doi in tried:
            continue
        tried.add(doi)
        meta = fetch_crossref(doi)
        if meta:
            meta["resolved_via"] = "pdf_text_doi"
            return meta

    if not skip_ocr:
        for doi in iter_ocr_doi_candidates(pdf_path, backend=ocr_backend):
            if doi in tried:
                continue
            tried.add(doi)
            meta = fetch_crossref(doi)
            if meta:
                meta["resolved_via"] = "ocr_doi"
                return meta

    clean_title_query = re.sub(r"[_\-\.]", " ", pdf_path.stem)
    meta = search_crossref_by_title(clean_title_query)
    if meta:
        meta["resolved_via"] = "fuzzy_title_search"
    return meta
