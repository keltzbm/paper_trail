import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    from tqdm import tqdm

    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

from paper_trail.config import COMPLETED_FILE, DISCOVERED_FILE, DOWNLOAD_DIR
from paper_trail.downloader import download_via_ui
from paper_trail.organizer import organize
from paper_trail.utils import load_urls

logger = logging.getLogger("downloader")


def mark_completed(url: str):
    """Appends a single URL to completed_urls.txt in a thread-safe manner."""
    with open(COMPLETED_FILE, "a", encoding="utf-8") as f:
        f.write(f"{url}\n")


def download_single_url(url: str, download_dir: Path) -> tuple[bool, str]:
    """Worker function: Spawns a thread-isolated Playwright instance to download one PDF.
    Logs details to downloader.log while remaining quiet on terminal.
    """
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            logger.info(f"Opening: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Check for pre-order / pre-release titles
            page_text = page.content().lower()
            if "pre-order" in page_text or "pre-release" in page_text:
                logger.info(f"  [i] Skipping pre-release title: {url}")
                mark_completed(url)
                browser.close()
                return True, url

            # Attempt PDF download
            saved_file = download_via_ui(page, str(download_dir))
            browser.close()

            if saved_file:
                logger.info(f"  --> Successfully saved ({saved_file}): {url}")
                mark_completed(url)
                return True, url
            else:
                logger.warning(
                    f"  [!] Failed to download PDF for {url}. Remaining in queue."
                )
                return False, url

    except Exception as e:
        logger.error(f"  [!] Error processing {url}: {e}")
        return False, url


def process_urls_concurrently(pending_urls: list[str], max_workers: int = 4):
    """Processes pending URLs concurrently using a ThreadPoolExecutor with a clean progress bar."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Temporarily raise terminal logger level to WARNING to silence INFO spam
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            handler.setLevel(logging.WARNING)

    success_count = 0
    failed_count = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(download_single_url, url, DOWNLOAD_DIR): url
            for url in pending_urls
        }

        if HAS_TQDM:
            progress = tqdm(
                as_completed(futures),
                total=len(pending_urls),
                desc="Downloading PDFs",
                unit="book",
                leave=True,
            )
            for future in progress:
                is_success, _ = future.result()
                if is_success:
                    success_count += 1
                else:
                    failed_count += 1
        else:
            for i, future in enumerate(as_completed(futures), 1):
                print(f"Downloading [{i}/{len(pending_urls)}]...", end="\r", flush=True)
                is_success, _ = future.result()
                if is_success:
                    success_count += 1
                else:
                    failed_count += 1

    print("\n" + "─" * 50)
    print(
        f"Download Pass Finished: {success_count} completed, {failed_count} failed/skipped."
    )


def cli_main():
    parser = argparse.ArgumentParser(
        description="Download pending PDFs from discovered URLs concurrently."
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=4,
        help="Number of concurrent download browser workers (default: 4)",
    )
    parser.add_argument(
        "--organize",
        "-o",
        action="store_true",
        help="Automatically organize PDFs into destination folder after downloading",
    )
    parser.add_argument(
        "--output-dir",
        default="/Users/keltzbm/pCloud Drive/research",
        help="Destination research folder for organized PDFs",
    )
    args = parser.parse_args()

    discovered = load_urls(DISCOVERED_FILE)
    completed = load_urls(COMPLETED_FILE)

    pending_urls = [u for u in discovered if u not in completed]
    print(
        f"Loaded {len(discovered)} book links. {len(completed)} previously processed."
    )
    print(
        f"Processing {len(pending_urls)} pending link(s) using {args.workers} worker(s)...\n"
    )

    if not pending_urls:
        print("No pending URLs to download!")
    else:
        process_urls_concurrently(pending_urls, max_workers=args.workers)

    # Optionally trigger organization pass on downloaded PDFs
    if args.organize:
        print("\n=== Starting automatic organization pass... ===")
        organize(
            inbox_dir=str(DOWNLOAD_DIR),
            output_dir=args.output_dir,
            max_workers=args.workers,
        )


if __name__ == "__main__":
    cli_main()
