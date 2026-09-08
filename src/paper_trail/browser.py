import logging

logger = logging.getLogger("downloader")


def dismiss_cookie_banner(page):
    """Dismisses cookie consent banners to prevent blocking click interactions."""
    banner_selectors = [
        'button:has-text("Accept all cookies")',
        'button:has-text("Accept all")',
        'button:has-text("Accept")',
        'button[data-cc-action="accept"]',
        "#onetrust-accept-btn-handler",
    ]

    for selector in banner_selectors:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(300)
                return
        except Exception:
            pass

    try:
        page.evaluate("""() => {
            const banners = document.querySelectorAll('.cc-banner, #onetrust-banner-sdk, .cc-overlay, div[class*="cookie"]');
            banners.forEach(el => el.remove());
        }""")
    except Exception:
        pass
