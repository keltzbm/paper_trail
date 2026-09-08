"""Organizes a local folder of already-downloaded PDFs into a research library.

Reads PDFs from an inbox directory, identifies each one (via
``paper_trail.metadata``), dedupes, renames/moves it into a
``YYYY_author_title`` folder, and writes an Obsidian-ready note alongside it.
Operates purely on local files; it has no knowledge of where the PDFs came
from and does no network fetching of its own beyond the Crossref lookups in
``paper_trail.metadata``.
"""

import argparse
import logging
import shutil
from collections import Counter
from pathlib import Path

from paper_trail.concurrency import default_worker_count, run_concurrent
from paper_trail.config import ORGANIZER_LOG, PROJECT_ROOT
from paper_trail.json_store import load_json_store, update_json_store
from paper_trail.metadata import get_metadata
from paper_trail.notes import generate_markdown_note
from paper_trail.utils import find_duplicates, make_folder_name

# Anchored to the project root (from config.py) rather than a path relative
# to wherever the command happens to be run from, so the cache lands in the
# same place regardless of your current directory.
CACHE_FILE = PROJECT_ROOT / "data" / "cache_index.json"


def setup_logger(verbose: bool = False) -> logging.Logger:
    """Configures the logger for organizer execution."""
    logger = logging.getLogger("paper_trail.organizer")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    file_handler = logging.FileHandler(ORGANIZER_LOG, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(file_handler)

    if verbose:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console_handler)

    return logger


def safe_mkdir(path: Path) -> None:
    """Creates directory safely if it does not exist."""
    path.mkdir(parents=True, exist_ok=True)


def _metadata_fields(meta) -> tuple:
    """Extracts (year, author, title, doi, resolved_via) whether meta is a dict or legacy tuple/list."""
    if isinstance(meta, dict):
        return (
            meta.get("year"), meta.get("author"), meta.get("title"),
            meta.get("doi"), meta.get("resolved_via"),
        )
    if isinstance(meta, (tuple, list)):
        return tuple(meta[i] if len(meta) > i else None for i in range(4)) + (None,)
    return None, None, None, None, None


def _move_companion_png(src_pdf: Path, dest_dir: Path, dest_stem: str) -> None:
    """Moves a PDF's companion thumbnail (same stem, .png) alongside it, if one exists."""
    src_png = src_pdf.with_suffix(".png")
    if src_png.exists():
        shutil.move(str(src_png), str(dest_dir / f"{dest_stem}.png"))


def process_single_pdf(
    pdf: Path, output: Path, dry_run: bool, logger: logging.Logger,
    skip_ocr: bool = False, ocr_backend: str = "auto", cache: dict | None = None,
) -> tuple[bool, Path, str | None]:
    """Processes a single PDF file: retrieves metadata, renames, and relocates.

    ``cache`` should be a dict already loaded once (via ``load_json_store``)
    and shared across every call in a batch — see ``organize()``. Loading it
    fresh here on every single call would mean re-reading and re-parsing an
    ever-growing JSON file from disk once per PDF, which gets slower as the
    run progresses. Falls back to a fresh load if called standalone.
    """
    logger.info(f"→ Starting: {pdf.name}")

    if cache is None:
        cache = load_json_store(CACHE_FILE)

    file_key = str(pdf.resolve())
    cached_meta = cache.get(file_key, {}).get("metadata")

    meta = cached_meta or get_metadata(pdf, skip_ocr=skip_ocr, ocr_backend=ocr_backend)
    if meta and not cached_meta:
        update_json_store(CACHE_FILE, file_key, {"metadata": meta}, store=cache)

    if not meta:
        logger.warning(f"✗ No metadata found for {pdf.name} — moving to _unfiled/")
        if not dry_run:
            unfiled_dir = output / "_unfiled"
            safe_mkdir(unfiled_dir)
            shutil.move(str(pdf), str(unfiled_dir / pdf.name))
            _move_companion_png(pdf, unfiled_dir, pdf.stem)
        return False, pdf, None

    year, author, title, doi, resolved_via = _metadata_fields(meta)
    folder_name = make_folder_name(year, author, title)
    dest_dir = output / folder_name
    dest_pdf = dest_dir / f"{folder_name}.pdf"

    logger.info(f"Organized: {pdf.name} → {folder_name}/ (resolved via: {resolved_via})")

    if not dry_run:
        safe_mkdir(dest_dir)
        shutil.move(str(pdf), str(dest_pdf))
        _move_companion_png(pdf, dest_dir, folder_name)

        generate_markdown_note(
            dest_dir, folder_name, year, author, title, doi=doi, resolved_via=resolved_via
        )

    return True, pdf, resolved_via


def organize(
    inbox_dir: str = "downloaded_pdfs",
    output_dir: str = "/Users/keltzbm/pCloud Drive/research",
    dry_run: bool = False,
    verbose: bool = False,
    max_workers: int | None = None,
    file_timeout: float | None = 90.0,
    skip_ocr: bool = False,
    ocr_backend: str = "auto",
) -> None:
    """Executes the multi-threaded PDF organization pipeline.

    ``max_workers``: if ``None`` (the default), concurrency *adapts* to
    system load throughout the run — see ``concurrency.run_concurrent``.
    Pass an explicit number to pin a fixed worker count instead.

    ``file_timeout`` bounds how long any single PDF is waited on (metadata
    lookup, OCR, cloud-drive reads, etc. can all stall). A file that exceeds
    it is reported and left in place rather than letting one bad file freeze
    progress on the rest of the batch. Pass ``None`` to wait indefinitely.
    """
    logger = setup_logger(verbose)
    inbox_path = Path(inbox_dir).expanduser().resolve()
    output_path = Path(output_dir).expanduser().resolve()

    worker_label = (
        f"{default_worker_count()} worker(s), auto-adjusting to system load"
        if max_workers is None
        else f"{max_workers} worker(s)"
    )

    if not inbox_path.exists():
        print(f"Inbox directory '{inbox_path}' does not exist.")
        return

    safe_mkdir(output_path)
    all_pdfs = list(inbox_path.glob("*.pdf"))

    if not all_pdfs:
        print(f"No PDFs found in inbox: {inbox_path}")
        return

    duplicates = set(find_duplicates(all_pdfs, max_workers=max_workers))
    pdfs = [p for p in all_pdfs if p not in duplicates]

    if duplicates:
        print(f"Skipped {len(duplicates)} duplicate(s) in inbox.")

    print(f"=== Starting Organization Run: {len(pdfs)} PDFs ({worker_label}) ===")

    # Loaded once here and shared by every worker call below, instead of
    # each of the (potentially thousands of) calls to process_single_pdf
    # re-reading and re-parsing this same, ever-growing file from disk.
    cache = load_json_store(CACHE_FILE)

    timed_out_pdfs: list[Path] = []

    def on_error(pdf: Path, exc: Exception) -> None:
        if isinstance(exc, TimeoutError):
            timed_out_pdfs.append(pdf)
            logger.warning(f"⏱ Gave up waiting on {pdf.name}: {exc}")
        else:
            logger.error(f"Worker exception ({pdf.name}): {exc}")

    results = run_concurrent(
        pdfs,
        lambda pdf: process_single_pdf(
            pdf, output_path, dry_run, logger,
            skip_ocr=skip_ocr, ocr_backend=ocr_backend, cache=cache,
        ),
        desc="Organizing PDFs",
        unit="pdf",
        max_workers=max_workers,
        on_error=on_error,
        timeout=file_timeout,
    )

    success = sum(1 for r in results if r and r[0])
    unfiled_count = len(results) - success - len(timed_out_pdfs)
    method_counts = Counter(r[2] for r in results if r and r[0] and r[2])

    print("\n" + "─" * 50)
    print(f"Organization finished! Success: {success} | Unfiled: {unfiled_count}", end="")
    if timed_out_pdfs:
        print(f" | Timed out, left in place: {len(timed_out_pdfs)} (see {ORGANIZER_LOG} for names)")
    else:
        print()
    if method_counts:
        breakdown = ", ".join(f"{method}={count}" for method, count in method_counts.most_common())
        print(f"  Resolved via: {breakdown}")
        for shaky in ("ocr_doi", "fuzzy_title_search"):
            if shaky in method_counts:
                print(
                    f"  ⚠ {method_counts[shaky]} matched via '{shaky}' — the least reliable "
                    "method here; worth a quick spot-check (search notes for "
                    f"'resolved_via: {shaky}')."
                )


def _read_resolved_via(note_path: Path) -> str | None:
    """Reads the ``resolved_via:`` field out of a note's frontmatter, if present."""
    for line in note_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("resolved_via:"):
            return line.split(":", 1)[1].strip()
    return None


def revert_matches(output_dir: str, method: str = "filename_doi", dry_run: bool = False) -> None:
    """Scans an already-organized library and moves entries matched via
    ``method`` back into ``_unfiled/`` for re-review.

    This reads the ``resolved_via`` field this tool writes into each
    paper's note frontmatter — it doesn't re-derive or re-verify anything,
    it just reverts based on what was recorded at organize time. Useful
    for methods you've decided not to trust (in fact, "DOI found directly
    in the filename" no longer runs at all going forward — see
    ``get_metadata`` — so this is specifically for cleaning up files that
    were already organized under it before that change).

    The file keeps its current (organized) name when moved back — its
    original pre-organize filename isn't tracked anywhere, so there's
    nothing reliable to restore it to.
    """
    output_path = Path(output_dir).expanduser().resolve()
    unfiled_dir = output_path / "_unfiled"

    if not output_path.exists():
        print(f"Library directory '{output_path}' does not exist.")
        return

    reverted = 0
    for folder in sorted(p for p in output_path.iterdir() if p.is_dir() and p.name != "_unfiled"):
        note_path = folder / f"{folder.name}.md"
        if not note_path.exists():
            continue

        resolved_via = _read_resolved_via(note_path)
        if resolved_via != method:
            continue

        pdf_path = folder / f"{folder.name}.pdf"
        if not pdf_path.exists():
            print(f"  ⚠ Skipping {folder.name}: expected {pdf_path.name} but it's missing.")
            continue

        print(f"Reverting: {folder.name} (matched via {resolved_via})")
        if not dry_run:
            safe_mkdir(unfiled_dir)
            shutil.move(str(pdf_path), str(unfiled_dir / pdf_path.name))

            png_path = folder / f"{folder.name}.png"
            if png_path.exists():
                shutil.move(str(png_path), str(unfiled_dir / png_path.name))

            note_path.unlink()
            try:
                folder.rmdir()  # only succeeds if now empty
            except OSError:
                pass
        reverted += 1

    verb = "Would revert" if dry_run else "Reverted"
    print(f"\n{verb} {reverted} file(s) matched via '{method}'.")


def cli_main():
    parser = argparse.ArgumentParser(
        description="Organize research PDFs into co-located paper folders concurrently."
    )
    parser.add_argument(
        "inbox",
        nargs="?",
        default="downloaded_pdfs",
        help="Folder containing unsorted PDFs (default: downloaded_pdfs)",
    )
    parser.add_argument(
        "output",
        nargs="?",
        default="/Users/keltzbm/pCloud Drive/research",
        help="Destination research folder (default: '/Users/keltzbm/pCloud Drive/research')",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=None,
        help=(
            "Pin a fixed number of concurrent worker threads. Default: "
            f"adaptive — starts at {default_worker_count()} on this machine "
            "right now, and re-checks system load every ~10s throughout the "
            "run, scaling up or down as load changes rather than staying "
            "fixed."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview actions without moving files or writing notes",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print full step-by-step logs to terminal",
    )
    parser.add_argument(
        "--file-timeout",
        type=float,
        default=90.0,
        help=(
            "Max seconds to wait on any single PDF (metadata lookup, OCR, "
            "slow/cloud-synced reads, etc.) before reporting it and moving "
            "on, so one stuck file can't freeze the whole run. Use 0 to "
            "wait indefinitely (default: 90)"
        ),
    )
    parser.add_argument(
        "--skip-ocr",
        action="store_true",
        help=(
            "Skip OCR fallback for scanned PDFs with no embedded text/DOI "
            "(by far the slowest step). Unresolved files land in _unfiled/ "
            "instead of being OCR'd; much faster on a large mixed batch."
        ),
    )
    parser.add_argument(
        "--ocr-backend",
        choices=["auto", "vision", "tesseract"],
        default="auto",
        help=(
            "OCR engine for scanned PDFs. 'auto' (default) uses Apple's "
            "Vision framework on macOS — GPU/Neural Engine accelerated — "
            "and falls back to Tesseract (CPU) elsewhere or if Vision isn't "
            "installed. 'vision' or 'tesseract' force one or the other."
        ),
    )
    parser.add_argument(
        "--revert-method",
        default=None,
        metavar="METHOD",
        help=(
            "Instead of organizing, scan `output` for already-organized "
            "papers matched via this resolution method (e.g. "
            "'filename_doi', 'ocr_doi', 'fuzzy_title_search') and move "
            "them back to _unfiled/ for re-review. Skips the inbox/organize "
            "pass entirely; respects --dry-run to preview first."
        ),
    )
    args = parser.parse_args()

    if args.revert_method:
        # `inbox`/`output` are both optional positionals; if the user gave
        # only one path (the natural way to invoke revert mode, since
        # there's no separate "inbox" concept here), argparse fills it
        # into `inbox`, not `output` — so prefer that one in this case
        # rather than silently falling back to the `output` default.
        library = args.output
        if args.output == parser.get_default("output") and args.inbox != parser.get_default("inbox"):
            library = args.inbox
        revert_matches(library, method=args.revert_method, dry_run=args.dry_run)
        return

    organize(
        inbox_dir=args.inbox,
        output_dir=args.output,
        dry_run=args.dry_run,
        verbose=args.verbose,
        max_workers=args.workers,
        file_timeout=args.file_timeout or None,
        skip_ocr=args.skip_ocr,
        ocr_backend=args.ocr_backend,
    )


if __name__ == "__main__":
    cli_main()
