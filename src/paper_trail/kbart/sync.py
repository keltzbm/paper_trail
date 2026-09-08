import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bs4 import BeautifulSoup

try:
    from tqdm import tqdm

    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

from paper_trail.cache import load_cache_index, update_cache_index
from paper_trail.config import KBART_CACHE_DIR
from paper_trail.http_client import get_http_session

PUBLIC_KBART_URL = "https://metadata.springernature.com/kbart"

logger = logging.getLogger("downloader")


def is_remote_file_updated(url: str, session) -> tuple[bool, dict]:
    """Checks remote file freshness via HTTP HEAD request."""
    try:
        res = session.head(url, timeout=5)
        if res.status_code == 200:
            last_mod = res.headers.get("Last-Modified", "")
            etag = res.headers.get("ETag", "")

            cache = load_cache_index()
            cached_info = cache.get(url, {})

            if cached_info and (
                cached_info.get("last_modified") == last_mod
                or cached_info.get("etag") == etag
            ):
                return False, {"last_modified": last_mod, "etag": etag}

            return True, {"last_modified": last_mod, "etag": etag}
    except Exception as e:
        logger.warning(f"  [!] HEAD check failed for {url}: {e}")

    return True, {}


def scrape_public_kbart_links() -> set[str]:
    """Fast static HTML parsing for Springer KBART links."""
    session = get_http_session()
    urls = set()
    try:
        res = session.get(PUBLIC_KBART_URL, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            for link in soup.find_all("a", href=True):
                href = link["href"].strip()
                if any(
                    ext in href.lower() for ext in [".txt", ".tsv", ".csv", "/kbart/"]
                ):
                    if href.startswith("/"):
                        href = "https://metadata.springernature.com" + href
                    urls.add(href)
    except Exception as e:
        logger.warning(f"Fast HTTP scraping failed: {e}")
    return urls


def download_single_kbart_file(args: tuple[str, int, int]) -> Path | None:
    """Worker task to check and download a single KBART file."""
    file_url, idx, total = args
    session = get_http_session()
    updated, meta = is_remote_file_updated(file_url, session)

    filename = file_url.split("/")[-1].split("?")[0]
    if not any(filename.endswith(ext) for ext in [".txt", ".tsv", ".csv"]):
        filename = f"kbart_list_{idx}.txt"

    dest_file = KBART_CACHE_DIR / filename

    if not updated and dest_file.exists():
        return dest_file

    try:
        res = session.get(file_url, stream=True, timeout=15)
        if res.status_code == 200:
            with open(dest_file, "wb") as f:
                f.writelines(res.iter_content(chunk_size=8192))
            update_cache_index(file_url, meta)
            return dest_file
    except Exception as e:
        logger.error(f"Failed downloading {file_url}: {e}")

    return None


def fetch_all_kbart_files(max_workers: int = 8) -> list[Path]:
    """Discovers, updates, and downloads all KBART lists concurrently with progress tracking."""
    KBART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    downloaded_files: list[Path] = []

    print("Fetching KBART link directory...")
    target_urls = scrape_public_kbart_links()

    if not target_urls:
        print("No URLs discovered from KBART portal.")
        return []

    print(
        f"Targeting {len(target_urls)} KBART endpoint(s) with {max_workers} workers..."
    )

    tasks = [
        (file_url, i, len(target_urls)) for i, file_url in enumerate(target_urls, 1)
    ]

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(download_single_kbart_file, task) for task in tasks]

        if HAS_TQDM:
            pbar = tqdm(
                total=len(tasks), desc="Syncing KBART Lists", unit="file", leave=True
            )
            for future in as_completed(futures):
                try:
                    res = future.result()
                    if res:
                        downloaded_files.append(res)
                finally:
                    pbar.update(1)
            pbar.close()
        else:
            for future in as_completed(futures):
                res = future.result()
                if res:
                    downloaded_files.append(res)

    print(f"Finished syncing {len(downloaded_files)} KBART file(s).")
    return downloaded_files


def main():
    parser = argparse.ArgumentParser(
        description="Sync and download all KBART lists concurrently."
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=8,
        help="Number of concurrent download workers (default: 8)",
    )
    args = parser.parse_args()
    fetch_all_kbart_files(max_workers=args.workers)


if __name__ == "__main__":
    main()
