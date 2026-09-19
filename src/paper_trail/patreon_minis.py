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

Defaults to a visible window. A headless run hit CAPTCHAs after ~50 posts
that a visible run hadn't -- unconfirmed as the actual cause (could just as
easily be request volume/velocity, unrelated to headless vs. headed), but
not a coincidence worth testing against a 300+ release crawl. If Patreon
throws a CAPTCHA while --headless is on anyway, there's nothing that can
solve it in a window that doesn't exist -- the script screenshots the
challenge and stops rather than hanging for 15 minutes, and tells you to
re-run with --no-headless to clear it live. --login-only always forces a
real window regardless of --headless, since there's no way to click
through a login otherwise.

It still automates repeated access to a platform whose ToS restrict
automated use, even for content you're already paying for. Given that:
  - keep --delay generous (default 4s between page loads / downloads)
  - don't run multiple instances in parallel against the same account
  - if Patreon shows a CAPTCHA / "verify you are human" page, the script
    pauses and waits for YOU to clear it in the visible window rather than
    retrying blindly
  - this is for your own personal archive, not redistribution

ATTACHMENT DETECTION
---------------------
Confirmed against a real post (Humblewood #1, id 119752872): this creator
writes a manual per-tier breakdown into the post body as a bulleted list,
each item holding a real download link shaped like
    https://www.patreon.com/file?h=<id>&m=<id>
right next to a decoy "Alternative downloading post" link that points to
an unrelated post, not a file. find_attachment_links() matches the /file
link whose own visible text says "Tier N" for your target tier, and
ignores the decoy. If a post has no tier breakdown at all (one file for
everyone), the sole /file link is used.

There's a second, UNCONFIRMED fallback for posts using Patreon's native
attachment system (CDN-hosted, ATTACHMENT_HREF_HINTS) rather than this
in-body-link style -- I haven't seen a real example of that pattern, so
if it's what actually fires for some posts, treat it as still-a-guess.
Posts where nothing matches get logged to needs_review instead of failing
silently, with their HTML dumped to <output-dir>/_review_html/ for a
quick `grep` to see what's actually there.

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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print(
        "Playwright isn't installed. Run:\n"
        "    pip install playwright --break-system-packages\n"
        "    playwright install chromium",
        file=sys.stderr,
    )
    sys.exit(1)


DEFAULT_CATALOGUE_URL = "https://sites.google.com/view/papermagecrafts/all-minis"

# Personal default -- override any time with --output-dir. Kept as a named
# constant (not inline in argparse) for the same reason config.py exists in
# paper_trail: one place to change it rather than hunting through the file.
DEFAULT_OUTPUT_DIR = "~/atelier/library/dnd/minis"

# Confirmed via a real post (see "ATTACHMENT DETECTION" above): the actual
# per-tier download links Patreon serves through the post body.
PATREON_FILE_LINK_RE = re.compile(r"patreon\.com/file\?", re.IGNORECASE)

# UNCONFIRMED fallback for posts using native Patreon attachments instead
# of the in-body /file? links above. See "ATTACHMENT DETECTION" note.
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
        groups.get(parent).push({ href: a.href, label: (a.textContent || '').trim() });
    }
    const result = [];
    for (const [parent, links] of groups.entries()) {
        result.push({ text: (parent.textContent || '').trim(), links });
    }
    return result;
}
"""

# textContent, not innerText -- innerText returns "" for anything sitting
# inside a CSS-collapsed "Continue Reading" fold, even though the element
# (and its href) genuinely exists in the DOM. textContent ignores rendering
# state entirely and reads the real text either way.
ALL_LINKS_JS = """
() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
    href: a.href,
    label: (a.textContent || '').trim(),
}))
"""

# Best-effort: expand a collapsed post body if one exists, so the tier
# links (and their labels) are actually there to find. Native .click()
# on the raw element, not a Playwright locator -- this trigger itself may
# be hidden/oddly-positioned and we don't need actionability guarantees
# for it, just for it to fire.
EXPAND_POST_JS = """
() => {
    const re = /continue reading|read more|show more|see more/i;
    const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'));
    for (const el of candidates) {
        if (re.test((el.textContent || '').trim())) {
            el.click();
            return true;
        }
    }
    return false;
}
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


def wait_for_manual_resolution(
    page, headless: bool, challenge_dir: Path, poll_s: int = 5, max_wait_s: int = 900
) -> None:
    if headless:
        # nothing you can do here -- there's no window to solve it in.
        # Save a screenshot and stop instead of polling for 15 minutes
        # against a challenge that can't possibly clear on its own.
        challenge_dir.mkdir(parents=True, exist_ok=True)
        shot_path = challenge_dir / f"challenge_{dt.datetime.now():%Y%m%d_%H%M%S}.png"
        try:
            page.screenshot(path=str(shot_path))
        except Exception:
            shot_path = None
        raise RuntimeError(
            "Verification/CAPTCHA page hit while running headless -- no "
            "window to solve it in."
            + (f" Screenshot saved to {shot_path}." if shot_path else "")
            + " Re-run with --no-headless, solve it once, then headless is"
            " fine again after."
        )

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


def find_attachment_links(page, tier: int, log: logging.Logger) -> List[Dict[str, str]]:
    links = page.evaluate(ALL_LINKS_JS)

    # Primary, confirmed pattern: per-tier /file?h=..&m=.. links embedded in
    # the post body, each one's own visible text naming its tier ("Tier 3").
    # A "📁 Alternative downloading post" link sits right next to each one
    # but points at a different post, not a file -- PATREON_FILE_LINK_RE
    # excludes it since its href is /posts/..., not /file?.
    file_links = [l for l in links if PATREON_FILE_LINK_RE.search(l["href"])]
    log.info("  %d patreon.com/file link(s) found on this post", len(file_links))
    if file_links:
        tier_matches = [l for l in file_links if tier in parse_tier_numbers(l.get("label", ""))]
        if tier_matches:
            return tier_matches
        if len(file_links) == 1:
            # no per-tier breakdown at all -- one file, open to everyone
            return file_links
        # tier links exist but none carry our tier number in their label --
        # don't guess which one to grab, flag for review instead
        log.info(
            "  none matched Tier %d -- labels seen: %s",
            tier,
            [l.get("label") or "(empty)" for l in file_links],
        )
        return []

    # Fallback: native Patreon attachments (CDN-hosted). Unconfirmed --
    # see the "ATTACHMENT DETECTION" note at the top of this file.
    return [
        l
        for l in links
        if any(hint in l["href"] for hint in ATTACHMENT_HREF_HINTS)
        or ATTACHMENT_EXT_RE.search(l["href"])
    ]


def flag_for_review(progress: dict, post_id: str, title: str, url: str, error: str) -> None:
    """Replace any prior review entry for this post_id rather than piling up
    duplicates across repeated runs against the same release."""
    progress["needs_review"] = [
        e for e in progress["needs_review"] if e.get("post_id") != post_id
    ]
    progress["needs_review"].append(
        {"post_id": post_id, "title": title, "url": url, "error": error}
    )


def dump_review_html(output_dir: Path, post_id: str, html: str) -> None:
    review_dir = output_dir / "_review_html"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / f"{post_id}.html").write_text(html, errors="ignore")


def download_release(
    context,
    page,
    release: Release,
    output_dir: Path,
    tier: int,
    delay: float,
    headless: bool,
    challenge_dir: Path,
    log: logging.Logger,
) -> List[Path]:
    post_dir = output_dir / f"{release.post_id}_{release.slug}"
    post_dir.mkdir(parents=True, exist_ok=True)

    page.goto(release.url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(int(delay * 1000))

    if is_challenge_page(page):
        log.warning("Verification challenge on %s", release.url)
        wait_for_manual_resolution(page, headless, challenge_dir)

    try:
        if page.evaluate(EXPAND_POST_JS):
            log.info("  expanded a collapsed post body")
            page.wait_for_timeout(1000)
    except Exception:
        pass

    candidates = find_attachment_links(page, tier, log)
    saved: List[Path] = []

    # Every candidate link has target="_blank". Whether that opens a new tab
    # or goes straight to a download varies, and either way the "download"
    # event might fire on a page other than `page`. Listening at the
    # CONTEXT level catches it regardless of which page triggers it.
    collected: List = []

    def _on_download(dl):
        collected.append(dl)

    context.on("download", _on_download)
    try:
        for link in candidates:
            before_pages = set(context.pages)
            before_count = len(collected)
            try:
                locator = page.locator(f'a[href="{link["href"]}"]').first
                locator.click(timeout=10000)
            except Exception as e:
                log.info("  couldn't click %s: %s", link["href"], e)
                continue

            deadline = time.time() + 20
            while time.time() < deadline and len(collected) == before_count:
                page.wait_for_timeout(250)

            # close any stray tab the click opened, whether or not it produced
            # a download, so tabs don't accumulate over a long run
            for p in set(context.pages) - before_pages:
                try:
                    p.close()
                except Exception:
                    pass

            if len(collected) > before_count:
                download = collected[-1]
                suggested = download.suggested_filename or (
                    Path(link["href"]).name or f"{release.slug}.bin"
                )
                dest = post_dir / suggested
                download.save_as(dest)
                saved.append(dest)
                log.info("  saved %s", dest.name)
            else:
                log.info(
                    "  no download event for %s (%s) -- may be a preview link",
                    link["href"],
                    link.get("label", ""),
                )
            page.wait_for_timeout(int(delay * 1000))
    finally:
        context.remove_listener("download", _on_download)

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
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--profile-dir", default="./.patreon-profile", help="Persistent Chromium profile (holds your login)")
    p.add_argument("--log-dir", default="./logs")
    p.add_argument(
        "--challenge-dir",
        default="./challenge_data",
        help="Where CAPTCHA screenshots land when hit while headless "
        "(named to fall under existing *data/ .gitignore rules)",
    )
    p.add_argument("--delay", type=float, default=4.0, help="Seconds between page loads / downloads (default: 4.0)")
    p.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Run without a visible window. Defaults to off (visible) -- a "
        "headless run hit CAPTCHAs after ~50 posts that a visible one hadn't; "
        "unconfirmed as the actual cause, but not worth risking on a 300+ "
        "release crawl. Use --headless once that's been tested and holds up.",
    )
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
    challenge_dir = Path(args.challenge_dir).expanduser().resolve()

    progress_path = output_dir / ".progress.json"
    progress = load_progress(progress_path)

    with sync_playwright() as pw:
        # --login-only always needs a real window regardless of --headless --
        # there's no way to click through Patreon's login in a headless one.
        effective_headless = False if args.login_only else args.headless
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            headless=effective_headless,
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
            wait_for_manual_resolution(page, args.headless, challenge_dir)

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
                saved = download_release(
                    context, page, r, output_dir, args.tier, args.delay,
                    args.headless, challenge_dir, log,
                )
            except Exception as e:
                log.exception("Failed on [%s] %s: %s", r.post_id, r.title, e)
                flag_for_review(progress, r.post_id, r.title, r.url, str(e))
                save_progress(progress_path, progress)
                continue

            if saved:
                progress["completed"][r.post_id] = {
                    "title": r.title,
                    "url": r.url,
                    "files": [str(f) for f in saved],
                    "downloaded_at": dt.datetime.now().isoformat(timespec="seconds"),
                }
                # a previous run may have flagged this one for review --
                # clear that now that it's actually succeeded
                progress["needs_review"] = [
                    e for e in progress["needs_review"] if e.get("post_id") != r.post_id
                ]
            else:
                flag_for_review(progress, r.post_id, r.title, r.url, "no attachments found")
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
