#!/usr/bin/env python3
"""
patreon_minis.py

Personal-archive downloader for the papermagecrafts Patreon papercraft-minis
catalogue, scoped to your own Patreon tier.

WHY THIS LOOKS DIFFERENT FROM arxiv.py
---------------------------------------
arxiv.py talks to a public API under arXiv's documented rate limits. This
script is different in kind: Patreon actively blocks plain scripted HTTP
requests (confirmed directly -- a bare unauthenticated GET to a post got
bot-blocked instantly), so there's no polite-requests-with-a-delay version
of this. Instead, this drives a REAL Chromium browser (via Playwright)
through YOUR OWN, already-logged-in Patreon session -- the same clicks
you'd make by hand, just unattended.

It deliberately does NOT:
  - spoof headers, rotate IPs, or fake being anything other than a real
    browser
  - bypass any paywall -- it only ever sees what your account already has
    access to, via your own real session cookie
  - run headless by default (a visible window makes the first login easy
    and makes it obvious if something looks wrong)

It still automates repeated access to a platform whose ToS restrict
automated use, even for content you're already paying for. Given that:
  - keep --delay generous (default 4s between page loads / downloads)
  - don't run multiple instances in parallel against the same account
  - if Patreon shows a CAPTCHA / "verify you are human" page, the script
    pauses and waits for YOU to clear it in the visible window rather than
    retrying blindly
  - this is for your own personal archive, not redistribution

CAVEAT ON ATTACHMENT DETECTION
-------------------------------
I couldn't inspect a real, logged-in Patreon post's DOM while building this
(same bot-block above applies to me too). The attachment-link detection in
find_attachment_links() is a best-effort, multi-strategy guess based on
Patreon's known CDN domains and common file extensions. Posts where nothing
matches get logged to needs_review instead of silently failing, and their
raw HTML gets dumped to <output-dir>/_review_html/ so you can grep the real
markup and adjust ATTACHMENT_HREF_HINTS in one place. Budget one short
calibration pass on your first real run (--limit 5 is good for this).

FIRST RUN (uv-managed project, e.g. paper_trail)
--------------------------------------------------
    uv add playwright
    uv run playwright install chromium

    uv run python patreon_minis.py --login-only

...opens a real Chromium window at patreon.com. Log in there once as
yourself; the session is saved under --profile-dir (default:
./.patreon-profile) and reused on every future run.

(No uv project? `pip install playwright --break-system-packages` and
`playwright install chromium` work too -- just drop the `uv run` prefix
below.)

CALIBRATION RUN (recommended before the full crawl)
-----------------------------------------------------
    uv run python patreon_minis.py --dry-run
    uv run python patreon_minis.py --limit 5

NORMAL RUN
----------
    uv run python patreon_minis.py

Crawls the catalogue page, visits every linked post, grabs the Tier 3
attachment(s), and saves them under --output-dir. Safe to Ctrl-C and
re-run -- progress is tracked in <output-dir>/.progress.json and
already-downloaded releases are skipped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

try:
    from playwright.sync_api import (
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ImportError:
    print(
        "Playwright isn't installed. Run:\n"
        "    pip install playwright --break-system-packages\n"
        "    playwright install chromium",
        file=sys.stderr,
    )
    sys.exit(1)


DEFAULT_CATALOGUE_URL = "https://sites.google.com/view/papermagecrafts/all-minis"

# Best-effort. See "CAVEAT ON ATTACHMENT DETECTION" above -- adjust after
# your first calibration run if needed.
ATTACHMENT_HREF_HINTS = [
    "patreonusercontent.com",
    "patreon-media.com",
    "c10.patreonusercontent.com",
    "downloads.patreon.com",
]
ATTACHMENT_EXT_RE = re.compile(r"\.(pdf|zip|rar|7z|png|jpe?g)(\?|$)", re.IGNORECASE)

CHALLENGE_INDICATORS = [
    "just a moment",
    "verify you are human",
    "checking your browser",
    "captcha",
    "unusual traffic",
    "attention required",
]

POST_ID_RE = re.compile(r"/posts/(?:[a-zA-Z0-9-]+-)?(\d+)")

GROUP_LINKS_JS = """
() => {
    const anchors = Array.from(document.querySelectorAll('a[href*="patreon.com/posts/"]'));
    const groups = new Map();
    for (const a of anchors) {
        const parent = a.parentElement || a;
        if (!groups.has(parent)) groups.set(parent, []);
        groups.get(parent).push({ href: a.href, label: (a.innerText || '').trim() });
    }
    const result = [];
    for (const [parent, links] of groups.entries()) {
        result.push({ text: (parent.innerText || '').trim(), links });
    }
    return result;
}
"""

ALL_LINKS_JS = """
() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
    href: a.href,
    label: (a.innerText || '').trim(),
}))
"""


@dataclass
class Release:
    index: int
    title: str
    slug: str
    post_id: str
    url: str


def setup_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("patreon_minis")
    log.setLevel(logging.INFO)
    log.handlers.clear()

    fh = logging.FileHandler(
        log_dir / f"patreon_minis_{dt.datetime.now():%Y%m%d_%H%M%S}.log"
    )
    sh = logging.StreamHandler()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:max_len].strip("-")


def extract_post_id(url: str) -> Optional[str]:
    m = POST_ID_RE.search(url)
    return m.group(1) if m else None


def parse_tier_numbers(label: str) -> List[int]:
    """'Tiers 1, 2, 3, 5 & 6' -> [1,2,3,5,6]; 'Tier 4 (cutfiles)' -> [4];
    'Visit post with all the download links' -> []"""
    nums = set()
    for m in re.finditer(r"\d+", label):
        n = int(m.group())
        if 1 <= n <= 20:
            nums.add(n)
    return sorted(nums)


def load_progress(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"completed": {}, "needs_review": []}


def save_progress(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def is_challenge_page(page) -> bool:
    try:
        title = (page.title() or "").lower()
        snippet = page.evaluate(
            "document.body ? document.body.innerText.slice(0, 500) : ''"
        ).lower()
    except Exception:
        return False
    haystack = f"{title} {snippet}"
    return any(ind in haystack for ind in CHALLENGE_INDICATORS)


def wait_for_manual_resolution(page, poll_s: int = 5, max_wait_s: int = 900) -> None:
    print("\n" + "=" * 60)
    print("Verification / CAPTCHA page detected.")
    print("Solve it in the visible browser window -- this will continue")
    print("automatically once the page looks normal again.")
    print("=" * 60 + "\n")
    waited = 0
    while waited < max_wait_s:
        page.wait_for_timeout(poll_s * 1000)
        waited += poll_s
        if not is_challenge_page(page):
            print("Looks clear -- continuing.\n")
            return
    raise RuntimeError("Timed out waiting for manual verification to clear.")


def scroll_to_load_all(page, max_scrolls: int = 40, pause_s: float = 0.6) -> None:
    """Google Sites can lazy-render long pages; scroll until height stabilizes."""
    last_height = 0
    stable_count = 0
    for _ in range(max_scrolls):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(int(pause_s * 1000))
        height = page.evaluate("document.body.scrollHeight")
        if height == last_height:
            stable_count += 1
            if stable_count >= 3:
                break
        else:
            stable_count = 0
        last_height = height


def collect_releases(page, tier: int, log: logging.Logger) -> List[Release]:
    raw_groups = page.evaluate(GROUP_LINKS_JS)
    releases: List[Release] = []
    seen_post_ids = set()
    unresolved = 0
    idx = 0

    for group in raw_groups:
        text = group.get("text", "") or ""
        links = group.get("links", []) or []
        if not links:
            continue

        title = text
        for link in links:
            if link.get("label"):
                title = title.replace(link["label"], "")
        title = re.sub(r"[|\[\]]+", " ", title)
        title = re.sub(r"\s+", " ", title).strip()

        tier_labeled = [l for l in links if parse_tier_numbers(l.get("label", ""))]
        chosen = None
        if tier_labeled:
            for l in tier_labeled:
                if tier in parse_tier_numbers(l["label"]):
                    chosen = l
                    break
        elif len(links) == 1:
            # single "Visit post with all the download links" style link --
            # Patreon renders that post based on the viewer's own pledge.
            chosen = links[0]

        if chosen is None:
            unresolved += 1
            log.warning(
                "No Tier %d link found for %r -- skipping (labels seen: %s)",
                tier,
                title[:80] or "(untitled)",
                [l.get("label") for l in links],
            )
            continue

        post_id = extract_post_id(chosen["href"])
        if not post_id or post_id in seen_post_ids:
            continue
        seen_post_ids.add(post_id)
        idx += 1
        releases.append(
            Release(
                index=idx,
                title=title[:120] or f"release {post_id}",
                slug=slugify(title) or f"release-{post_id}",
                post_id=post_id,
                url=chosen["href"],
            )
        )

    if unresolved:
        log.info(
            "%d release(s) had no explicit Tier %d link and weren't a single "
            "generic link either -- see warnings above.",
            unresolved,
            tier,
        )
    return releases


def find_attachment_links(page) -> List[Dict[str, str]]:
    links = page.evaluate(ALL_LINKS_JS)
    return [
        l
        for l in links
        if any(hint in l["href"] for hint in ATTACHMENT_HREF_HINTS)
        or ATTACHMENT_EXT_RE.search(l["href"])
    ]


def dump_review_html(output_dir: Path, post_id: str, html: str) -> None:
    review_dir = output_dir / "_review_html"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / f"{post_id}.html").write_text(html, errors="ignore")


def download_release(
    context,
    page,
    release: Release,
    output_dir: Path,
    delay: float,
    log: logging.Logger,
) -> List[Path]:
    post_dir = output_dir / f"{release.post_id}_{release.slug}"
    post_dir.mkdir(parents=True, exist_ok=True)

    page.goto(release.url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(int(delay * 1000))

    if is_challenge_page(page):
        log.warning("Verification challenge on %s", release.url)
        wait_for_manual_resolution(page)

    candidates = find_attachment_links(page)
    saved: List[Path] = []

    for link in candidates:
        try:
            locator = page.locator(f'a[href="{link["href"]}"]').first
            with page.expect_download(timeout=30000) as dl_info:
                locator.click()
            download = dl_info.value
            suggested = download.suggested_filename or (
                Path(link["href"]).name or f"{release.slug}.bin"
            )
            dest = post_dir / suggested
            download.save_as(dest)
            saved.append(dest)
            log.info("  saved %s", dest.name)
            page.wait_for_timeout(int(delay * 1000))
        except PlaywrightTimeoutError:
            # matched a plausible attachment domain/extension but clicking it
            # didn't trigger a browser download event -- likely a preview
            # link rather than a real attachment. Not fatal.
            log.info(
                "  %s (%s) matched but didn't download -- probably a preview link",
                link["href"],
                link.get("label", ""),
            )
        except Exception as e:
            log.info("  couldn't download %s: %s", link["href"], e)

    if not saved:
        try:
            dump_review_html(output_dir, release.post_id, page.content())
        except Exception:
            pass
        log.warning(
            "No attachments found for [%s] %s -- HTML dumped to "
            "_review_html/%s.html for manual inspection",
            release.post_id,
            release.title,
            release.post_id,
        )

    return saved


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--catalogue-url", default=DEFAULT_CATALOGUE_URL)
    p.add_argument("--tier", type=int, default=3, help="Patreon tier to download (default: 3)")
    p.add_argument("--output-dir", default="./patreon_minis")
    p.add_argument("--profile-dir", default="./.patreon-profile", help="Persistent Chromium profile (holds your login)")
    p.add_argument("--log-dir", default="./logs")
    p.add_argument("--delay", type=float, default=4.0, help="Seconds between page loads / downloads (default: 4.0)")
    p.add_argument("--headless", action="store_true", help="Run without a visible window (do a normal run first)")
    p.add_argument("--login-only", action="store_true", help="Just open a browser to log in, then exit")
    p.add_argument("--dry-run", action="store_true", help="List what would be fetched, download nothing")
    p.add_argument("--limit", type=int, default=None, help="Only process the first N releases (good for calibration)")
    p.add_argument("--only", default=None, help="Comma-separated Patreon post IDs to process")
    p.add_argument("--debug-dump-html", default=None, help="Dump the catalogue page HTML to this path and continue")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    log = setup_logger(Path(args.log_dir).expanduser().resolve())

    profile_dir = Path(args.profile_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    progress_path = output_dir / ".progress.json"
    progress = load_progress(progress_path)

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=args.headless,
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()

        if args.login_only:
            page.goto("https://www.patreon.com/login", wait_until="domcontentloaded")
            input(
                "Log in to Patreon in the opened browser window, then press "
                "Enter here to save the session... "
            )
            context.close()
            log.info("Session saved to %s", profile_dir)
            return

        log.info("Loading catalogue: %s", args.catalogue_url)
        page.goto(args.catalogue_url, wait_until="domcontentloaded", timeout=60000)
        scroll_to_load_all(page)

        if args.debug_dump_html:
            Path(args.debug_dump_html).write_text(page.content(), errors="ignore")
            log.info("Dumped catalogue HTML to %s", args.debug_dump_html)

        if is_challenge_page(page):
            log.warning("Verification challenge on the catalogue page itself")
            wait_for_manual_resolution(page)

        releases = collect_releases(page, args.tier, log)
        log.info("Found %d release(s) with a Tier %d link", len(releases), args.tier)

        if args.only:
            wanted = {s.strip() for s in args.only.split(",") if s.strip()}
            releases = [r for r in releases if r.post_id in wanted]
        if args.limit:
            releases = releases[: args.limit]

        if args.dry_run:
            for r in releases:
                status = "skip (done)" if r.post_id in progress["completed"] else "would fetch"
                print(f"[{status}] {r.post_id}  {r.title}  ->  {r.url}")
            context.close()
            return

        for r in releases:
            if r.post_id in progress["completed"]:
                log.info("Skipping (already done): [%s] %s", r.post_id, r.title)
                continue

            log.info("Fetching [%s/%s] %s", r.index, len(releases), r.title)
            try:
                saved = download_release(context, page, r, output_dir, args.delay, log)
            except Exception as e:
                log.exception("Failed on [%s] %s: %s", r.post_id, r.title, e)
                progress["needs_review"].append(
                    {"post_id": r.post_id, "title": r.title, "url": r.url, "error": str(e)}
                )
                save_progress(progress_path, progress)
                continue

            if saved:
                progress["completed"][r.post_id] = {
                    "title": r.title,
                    "url": r.url,
                    "files": [str(f) for f in saved],
                    "downloaded_at": dt.datetime.now().isoformat(timespec="seconds"),
                }
            else:
                progress["needs_review"].append(
                    {
                        "post_id": r.post_id,
                        "title": r.title,
                        "url": r.url,
                        "error": "no attachments found",
                    }
                )
            save_progress(progress_path, progress)
            page.wait_for_timeout(int(args.delay * 1000))

        context.close()

    done = len(progress["completed"])
    review = len(progress["needs_review"])
    log.info("Done. %d release(s) downloaded, %d flagged for manual review.", done, review)
    if review:
        log.info("Review list: %s", progress_path)


if __name__ == "__main__":
    main()
