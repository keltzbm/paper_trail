import argparse
import glob
import hashlib
import os
import re
import shutil
from pathlib import Path

from paper_trail.concurrency import default_worker_count, run_concurrent

STOPWORDS = {
    "a",
    "an",
    "the",
    "on",
    "of",
    "for",
    "and",
    "in",
    "to",
    "with",
    "by",
    "at",
    "from",
}


def load_urls(file_path: Path) -> set[str]:
    """Reads non-empty lines from a text file into a set."""
    if not file_path.exists():
        return set()
    with open(file_path, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def resolve_file_paths(file_inputs: list[str]) -> list[Path]:
    """Expands wildcards (~, *, ?) and resolves input paths into a sorted list of unique Path objects."""
    resolved_paths = []
    for item in file_inputs:
        expanded_pattern = os.path.expanduser(item)
        matched_files = glob.glob(expanded_pattern)

        if matched_files:
            for m in matched_files:
                p = Path(m)
                if p.is_file():
                    resolved_paths.append(p)
        else:
            p = Path(item).expanduser()
            if p.is_file():
                resolved_paths.append(p)

    return sorted(set(resolved_paths))


# ----------------------------------------------------------------------
# Local-library helpers (used by organizer.py to file already-downloaded PDFs)
# ----------------------------------------------------------------------
def slugify(text: str, max_words: int = 6) -> str:
    """Safely converts text to a clean URL/filesystem-safe slug."""
    if not text:
        return ""
    text = re.sub(r"\$.*?\$", "", text)
    text = re.sub(r"[^\w\s]", " ", text.lower())
    words = [w for w in text.split() if w and w not in STOPWORDS]
    return "_".join(words[:max_words])


def make_folder_name(
    year: str | int | None, author: str | None, title: str | None
) -> str:
    """Generates a folder name in YYYY_author_title format."""
    year_str = str(year).strip() if year else ""
    author_clean = ""
    if author and author.strip():
        first_author = re.split(r"[,;]", author)[0].strip()
        author_parts = first_author.split()
        if author_parts:
            author_clean = slugify(author_parts[-1], max_words=1)

    title_clean = slugify(title or "", max_words=6)
    parts = [p for p in (year_str, author_clean, title_clean) if p]

    return "_".join(parts) if parts else "untitled_paper"


def calculate_md5(file_path: Path, block_size: int = 65536) -> str:
    """Calculates MD5 hash of a file."""
    hasher = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(block_size), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def find_duplicates(file_paths: list[Path], max_workers: int | None = None) -> list[Path]:
    """Scans a list of PDF paths and returns duplicate files based on MD5 checksums.

    Files are grouped by size first (one cheap ``stat()`` call each) before
    any hashing happens — two files of different sizes can never be
    byte-identical, so only files that share a size with at least one other
    file are actually read and hashed. In a large, varied library that's
    normally a small fraction of the total, which skips reading the full
    contents of most files entirely.

    Hashing (of just those candidates) runs concurrently and reports
    progress. This pass used to be single-threaded with no progress output
    at all, which made it easy to mistake for a hang on a large inbox —
    especially over a cloud-synced drive (pCloud, Dropbox, etc.) where
    reading a file's bytes for the first time can trigger an on-demand
    download.

    ``max_workers``: if ``None`` (the default), concurrency *adapts* to
    system load throughout the scan — see ``concurrency.run_concurrent``.
    """
    files = [p for p in file_paths if p.is_file()]

    by_size: dict[int, list[Path]] = {}
    for path in files:
        by_size.setdefault(path.stat().st_size, []).append(path)

    candidates = [p for group in by_size.values() if len(group) > 1 for p in group]

    if not candidates:
        return []

    worker_label = (
        f"{default_worker_count()} worker(s), auto-adjusting to system load"
        if max_workers is None
        else f"{max_workers} worker(s)"
    )
    print(f"Hashing {len(candidates)} candidate file(s) for duplicates ({worker_label})...")

    def hash_one(path: Path) -> tuple[Path, str]:
        return path, calculate_md5(path)

    results = run_concurrent(
        candidates,
        hash_one,
        desc="Scanning for duplicates",
        unit="file",
        max_workers=max_workers,
    )

    hashes = {path: file_hash for r in results if r for path, file_hash in [r]}

    # Walk in original order so "which copy counts as the duplicate" stays
    # deterministic even though hashing itself ran out of order.
    seen_hashes = set()
    duplicates = []
    for path in file_paths:
        file_hash = hashes.get(path)
        if file_hash is None:
            continue
        if file_hash in seen_hashes:
            duplicates.append(path)
        else:
            seen_hashes.add(file_hash)
    return duplicates


def _move_single_item(item: Path, dst: Path) -> tuple[bool, str]:
    """Worker function to move a single file or directory to dst."""
    target_path = dst / item.name
    if target_path.exists():
        return False, item.name
    try:
        shutil.move(str(item), str(target_path))
        return True, item.name
    except Exception as e:
        return False, f"{item.name} (Error: {e})"


def move_library(
    source_dir: str, target_dir: str, max_workers: int | None = None, cleanup: bool = True
) -> None:
    """Moves organized paper folders concurrently and cleans up the source folder when done.

    ``max_workers``, if ``None`` (the default), is freshly computed from
    current CPU count and load average — see
    ``max_workers``: if ``None`` (the default), concurrency *adapts* to
    system load throughout the move — see ``concurrency.run_concurrent``.
    """
    src = Path(source_dir).expanduser().resolve()
    dst = Path(target_dir).expanduser().resolve()

    if not src.exists():
        print(f"Error: Source directory '{src}' does not exist.")
        return

    dst.mkdir(parents=True, exist_ok=True)
    items = list(src.iterdir())

    if not items:
        print(f"Source directory '{src}' is empty.")
        if cleanup:
            _cleanup_if_empty(src)
        return

    worker_label = (
        f"{default_worker_count()} worker(s), auto-adjusting to system load"
        if max_workers is None
        else f"{max_workers} worker(s)"
    )
    print(f"=== Relocating Library ({len(items)} items): {src} → {dst} using {worker_label} ===")

    results = run_concurrent(
        items,
        lambda item: _move_single_item(item, dst),
        desc="Relocating Papers",
        unit="folder",
        max_workers=max_workers,
    )
    moved = sum(1 for r in results if r and r[0])
    skipped = len(results) - moved

    print("\n" + "─" * 50)
    print(f"Done! Moved {moved} item(s) to {dst}. (Skipped/Failed {skipped} existing).")

    if cleanup:
        _cleanup_if_empty(src)


def _cleanup_if_empty(src: Path) -> None:
    """Removes src if (and only if) it's now empty, reporting what happened."""
    remaining = list(src.iterdir())
    if remaining:
        print(
            f"ℹ️ Source folder '{src}' still contains {len(remaining)} skipped/unmoved item(s); left intact."
        )
        return
    try:
        src.rmdir()
        print(f"🧹 Cleaned up and removed empty source folder: {src}")
    except Exception as e:
        print(f"⚠️ Could not remove source folder '{src}': {e}")


def cli_main():
    parser = argparse.ArgumentParser(
        description="Relocate organized papers concurrently and clean up empty source directory."
    )
    parser.add_argument(
        "source",
        help="Current library folder (e.g., ~/research)",
    )
    parser.add_argument(
        "target",
        help="Destination library folder (e.g., '/Users/keltzbm/pCloud Drive/research')",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=None,
        help=(
            "Pin a fixed number of concurrent relocation workers. Default: "
            f"adaptive — starts at {default_worker_count()} on this machine "
            "right now, and re-checks system load every ~10s throughout "
            "the run."
        ),
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Keep the empty source directory after moving files",
    )
    args = parser.parse_args()
    move_library(
        args.source,
        args.target,
        max_workers=args.workers,
        cleanup=not args.no_cleanup,
    )


if __name__ == "__main__":
    cli_main()
