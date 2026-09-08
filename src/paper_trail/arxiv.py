import argparse
import json
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

try:
    from tqdm import tqdm

    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

from paper_trail.config import CACHE_INDEX, DOWNLOAD_DIR

# arXiv's API Terms of Use (https://info.arxiv.org/help/api/tou.html) ask
# for "no more than one request every three seconds, and limit requests to
# a single connection at a time" against export.arxiv.org. This must hold
# across retries too, not just between successful pages — the previous
# retry backoff (2 * attempt) allowed a 2-second gap on the first retry,
# under the floor.
_ARXIV_API_MIN_INTERVAL = 3.0
_last_arxiv_api_request_time = 0.0

# arXiv's Bulk Data guidance (https://info.arxiv.org/help/bulk_data.html)
# says they prioritize interactive human users on the main site and ask
# automated tools to keep load conservative there — for large-scale needs
# they point to dedicated bulk mechanisms (S3, OAI-PMH) rather than
# concurrent downloads of individual PDFs, which is what this module does
# for a modest, user-requested count of specific papers. Downloads are
# sequential with a delay between each, not concurrent.
_DEFAULT_DOWNLOAD_DELAY = 1.0


def _wait_for_arxiv_api_rate_limit() -> None:
    """Blocks as needed so consecutive requests to export.arxiv.org are at
    least ``_ARXIV_API_MIN_INTERVAL`` seconds apart, per arXiv's API Terms
    of Use. Call this immediately before every request to that endpoint,
    including retries.
    """
    global _last_arxiv_api_request_time
    elapsed = time.monotonic() - _last_arxiv_api_request_time
    remaining = _ARXIV_API_MIN_INTERVAL - elapsed
    if remaining > 0:
        time.sleep(remaining)
    _last_arxiv_api_request_time = time.monotonic()


def load_cache() -> dict:
    if CACHE_INDEX.exists():
        try:
            with open(CACHE_INDEX, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def update_cache(paper_id: str, metadata: dict):
    CACHE_INDEX.parent.mkdir(parents=True, exist_ok=True)
    cache = load_cache()
    cache[paper_id] = metadata
    try:
        with open(CACHE_INDEX, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Failed to update cache: {e}")


def is_already_archived(paper_id: str, title: str, output_path: Path, cache: dict) -> bool:
    """Checks if paper is recorded in the cache index OR present on disk."""
    if paper_id in cache:
        return True

    clean_title = "".join(
        c for c in title if c.isalnum() or c in (" ", "_", "-")
    ).rstrip()
    filename = f"{clean_title[:80]}.pdf"
    dest_file = output_path / filename

    if dest_file.exists():
        update_cache(paper_id, {"title": title, "file": str(dest_file)})
        return True

    return False


def download_single_arxiv_paper(
    entry_data: dict, output_path: Path, session: requests.Session, max_attempts: int = 2
) -> bool:
    """Downloads a single arXiv PDF and updates the cache.

    Retries a couple of times on transient failures (arXiv's servers can
    be slow to respond occasionally), with a short backoff between
    attempts on top of the caller's between-download delay.
    """
    paper_id = entry_data["paper_id"]
    title = entry_data["title"]

    clean_title = "".join(
        c for c in title if c.isalnum() or c in (" ", "_", "-")
    ).rstrip()
    filename = f"{clean_title[:80]}.pdf"
    dest_file = output_path / filename

    pdf_url = f"https://arxiv.org/pdf/{paper_id}.pdf"

    for attempt in range(1, max_attempts + 1):
        try:
            res = session.get(pdf_url, stream=True, timeout=30)
            if res.status_code == 200:
                with open(dest_file, "wb") as f:
                    f.writelines(res.iter_content(chunk_size=8192))

                update_cache(
                    paper_id,
                    {"title": title, "file": str(dest_file), "source": "arxiv"},
                )
                return True
            print(f"  [Attempt {attempt}/{max_attempts}] {paper_id}: HTTP {res.status_code}")
        except Exception as e:
            print(f"  [Attempt {attempt}/{max_attempts}] Failed to download {paper_id}: {e}")

        if attempt < max_attempts:
            time.sleep(2 * attempt)

    return False


def fetch_uncollected_arxiv_entries(
    query: str,
    target_count: int,
    output_path: Path,
    cache: dict,
    batch_size: int = 50,
) -> list[dict]:
    """Paginates through arXiv results to find `target_count` uncollected papers."""
    uncollected_entries = []
    start = 0
    formatted_query = query

    print(f"Searching arXiv for uncollected papers matching: {formatted_query}...")

    with requests.Session() as session:
        while len(uncollected_entries) < target_count:
            params = {
                "search_query": formatted_query,
                "start": start,
                "max_results": batch_size,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
            url = f"https://export.arxiv.org/api/query?{urllib.parse.urlencode(params)}"

            response = None
            for attempt in range(1, 4):
                _wait_for_arxiv_api_rate_limit()
                try:
                    response = session.get(url, timeout=30)
                    response.raise_for_status()
                    break
                except Exception as e:
                    print(f"  [Attempt {attempt}/3] arXiv API request error: {e}")

            if not response:
                print("Aborting search due to persistent arXiv API timeout.")
                break

            root = ET.fromstring(response.content)
            namespace = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall("atom:entry", namespace)

            if not entries:
                print("No more results returned from arXiv.")
                break

            for entry in entries:
                id_elem = entry.find("atom:id", namespace)
                paper_id = (
                    id_elem.text.split("/abs/")[-1] if id_elem is not None else ""
                )
                title_elem = entry.find("atom:title", namespace)
                title = (
                    title_elem.text.strip().replace("\n", " ")
                    if title_elem is not None
                    else f"paper_{len(uncollected_entries) + 1}"
                )

                if is_already_archived(paper_id, title, output_path, cache):
                    continue

                if any(item["paper_id"] == paper_id for item in uncollected_entries):
                    continue

                uncollected_entries.append({"paper_id": paper_id, "title": title})

                if len(uncollected_entries) == target_count:
                    break

            start += batch_size

    return uncollected_entries


def download_arxiv_papers(
    query: str,
    target_count: int = 10,
    output_dir: Path | str | None = None,
    delay: float = _DEFAULT_DOWNLOAD_DELAY,
):
    """Finds and downloads uncollected arXiv PDFs matching `query`.

    Downloads happen one at a time with ``delay`` seconds between each —
    see the module-level notes on arXiv's bulk-access guidance for why
    this isn't a concurrent download pool. For genuinely large-scale needs
    (far beyond a handful of specific papers), arXiv's own recommendation
    is their bulk data mechanisms (S3, OAI-PMH), not scraping individual
    PDF URLs; this function doesn't attempt to replace those.
    """
    output_path = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else DOWNLOAD_DIR
    )
    output_path.mkdir(parents=True, exist_ok=True)

    cache = load_cache()

    uncollected_papers = fetch_uncollected_arxiv_entries(
        query, target_count, output_path, cache
    )

    if not uncollected_papers:
        print("No uncollected papers retrieved.")
        return

    print(
        f"Found {len(uncollected_papers)} uncollected paper(s). "
        f"Downloading to '{output_path.name}' ({delay:.1f}s between requests)..."
    )

    downloaded_count = 0
    total = len(uncollected_papers)
    iterator = (
        tqdm(uncollected_papers, desc="Fetching Papers", unit="paper", leave=True)
        if HAS_TQDM
        else uncollected_papers
    )

    with requests.Session() as session:
        for i, entry_data in enumerate(iterator):
            if i > 0:
                time.sleep(delay)

            if download_single_arxiv_paper(entry_data, output_path, session):
                downloaded_count += 1

            if not HAS_TQDM:
                print(f"Fetching Papers [{i + 1}/{total}]...", end="\r", flush=True)

    if not HAS_TQDM:
        print()

    print(f"Successfully downloaded {downloaded_count} paper(s) to {output_path}!")


def cli_main():
    parser = argparse.ArgumentParser(
        description="Fetch uncollected arXiv papers directly to staging folder."
    )
    parser.add_argument("query", type=str, help="Search query or author name")
    parser.add_argument(
        "--max",
        "-m",
        type=int,
        default=10,
        help="Number of uncollected papers to fetch (default: 10)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help=f"Destination directory (default: {DOWNLOAD_DIR})",
    )
    parser.add_argument(
        "--delay",
        "-d",
        type=float,
        default=_DEFAULT_DOWNLOAD_DELAY,
        help=(
            "Seconds to wait between each PDF download (default: "
            f"{_DEFAULT_DOWNLOAD_DELAY}). Downloads are sequential, not "
            "concurrent — arXiv asks automated tools to keep load on the "
            "main site conservative and prioritizes interactive human "
            "users; see https://info.arxiv.org/help/bulk_data.html."
        ),
    )
    args = parser.parse_args()

    download_arxiv_papers(args.query, args.max, args.output, args.delay)


if __name__ == "__main__":
    cli_main()
