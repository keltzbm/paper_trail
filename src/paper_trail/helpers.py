import datetime
import re
import time

from playwright.sync_api import sync_playwright

from paper_trail.config import COMPLETED_FILE, DISCOVERED_FILE
from paper_trail.utils import load_urls


def is_book_forthcoming(page) -> tuple[bool, str]:
    html_content = page.content().lower()
    forthcoming_keywords = [
        "forthcoming",
        "pre-order",
        "expected publication",
        "available soon",
        "this book is not yet published",
    ]
    for kw in forthcoming_keywords:
        if kw in html_content:
            return True, f"Matched keyword '{kw}'"

    pub_date_meta = page.query_selector('meta[name="citation_publication_date"]')
    if pub_date_meta and pub_date_meta.get_attribute("content"):
        pub_date_str = pub_date_meta.get_attribute("content").strip()
        match = re.search(r"\b(20\d{2})\b", pub_date_str)
        if match and int(match.group(1)) > datetime.datetime.now().year:
            return True, f"Future publication year ({pub_date_str})"

    return False, ""


def extract_direct_pdf_url(page, book_url: str) -> str:
    doi_element = page.query_selector('meta[name="citation_doi"]')
    if doi_element and doi_element.get_attribute("content"):
        doi = doi_element.get_attribute("content").strip()
        return f"https://link.springer.com/content/pdf/{doi}.pdf"

    book_id = book_url.rstrip("/").split("/")[-1]
    return f"https://link.springer.com/content/pdf/10.1007/{book_id}.pdf"


def mark_url_completed(
    book_url: str, completed_set: set, progress_file: str = str(COMPLETED_FILE)
):
    completed_set.add(book_url)
    with open(progress_file, "a", encoding="utf-8") as f:
        f.write(f"{book_url}\n")


def crawl_springer_az(
    letters: str = "abcdefghijklmnopqrstuvwxyz", max_pages_per_letter: int = 5
):
    existing_urls = load_urls(DISCOVERED_FILE)

    print("=== Starting A-Z Springer Crawler ===")
    print(f"Currently tracking {len(existing_urls)} URLs in discovered_urls.txt\n")

    new_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for char in letters:
            print(f"--- Crawling Letter: {char.upper()} ---")
            for page_num in range(1, max_pages_per_letter + 1):
                url = f"https://link.springer.com/search/page/{page_num}?facet-content-type=%22Book%22&query={char}"

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(1)

                    links = page.query_selector_all('a[href*="/book/"]')
                    found_urls = []

                    for link in links:
                        href = link.get_attribute("href")
                        if href:
                            clean_url = (
                                "https://link.springer.com"
                                + href.split("?")[0].split("#")[0]
                            )
                            if (
                                clean_url not in existing_urls
                                and clean_url not in found_urls
                            ):
                                found_urls.append(clean_url)
                                existing_urls.add(clean_url)

                    if found_urls:
                        with open(DISCOVERED_FILE, "a", encoding="utf-8") as f:
                            f.writelines(f"{book_url}\n" for book_url in found_urls)
                        new_count += len(found_urls)
                        print(f"  Page {page_num}: Added {len(found_urls)} new links.")

                except Exception as e:
                    print(f"  Error on page {page_num}: {e}")
                    break

        browser.close()

    print(f"\nDone! Added {new_count} total new book URLs to discovered_urls.txt.")


if __name__ == "__main__":
    crawl_springer_az()
