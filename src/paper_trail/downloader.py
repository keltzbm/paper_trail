import logging
import os

from paper_trail.browser import dismiss_cookie_banner
from paper_trail.config import DOWNLOADER_LOG


def setup_logger():
    """Configures logging to terminal output and downloader.log at project root."""
    logger = logging.getLogger("downloader")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    file_handler = logging.FileHandler(DOWNLOADER_LOG, mode="a", encoding="utf-8")
    file_formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)

    return logger


logger = setup_logger()


def download_via_ui(page, download_dir: str) -> str | None:
    dismiss_cookie_banner(page)

    pdf_selector = (
        'a[href*="/content/pdf/"], '
        'a[href*="/download/epub/"], '
        "a.c-pdf-download__link, "
        'a[data-test="pdf-link"], '
        'a[data-track-action="book pdf download"], '
        'a:has-text("Download book PDF"), '
        'a:has-text("Download PDF")'
    )

    try:
        page.wait_for_selector(pdf_selector, timeout=4000)
        download_btn = page.query_selector(pdf_selector)
    except Exception:
        download_btn = None

    if download_btn:
        try:
            with page.expect_download(timeout=30000) as download_info:
                download_btn.click(force=True)

            download = download_info.value
            filename = download.suggested_filename

            if filename in (
                "1.pdf",
                "fulltext.pdf",
                "content.pdf",
            ) or not filename.endswith(".pdf"):
                url_part = (
                    page.url.split("/")[-1]
                    .replace("10.1007-", "")
                    .replace("10.1007_", "")
                )
                filename = f"{url_part}.pdf"

            save_path = os.path.join(download_dir, filename)
            download.save_as(save_path)

            logger.info(f"  --> Successfully saved via UI: {filename}")
            return filename
        except Exception as e:
            logger.warning(f"  [!] UI download click failed: {e}")

    return None


def download_via_http(context, direct_pdf_url: str, download_dir: str) -> bool:
    """Attempts to fetch PDF bytes directly via Playwright's HTTP context."""
    logger.info(f"  [i] Fetching directly via HTTP context: {direct_pdf_url}")

    try:
        response = context.request.get(direct_pdf_url)

        if response.status == 200 and "application/pdf" in response.headers.get(
            "content-type", ""
        ):
            file_name = direct_pdf_url.split("/")[-1]
            save_path = os.path.join(download_dir, file_name)

            with open(save_path, "wb") as f:
                f.write(response.body())

            logger.info(f"  --> Successfully saved via direct HTTP: {file_name}")
            return True

        logger.warning(f"  [!] HTTP {response.status}: Asset unavailable or paywalled.")
    except Exception as e:
        logger.error(f"  [!] HTTP request error: {e}")

    return False
