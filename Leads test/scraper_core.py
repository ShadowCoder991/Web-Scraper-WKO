#!/usr/bin/env python3
"""
Reusable scraping engine — shared by every source-specific adapter
(adapters/wko.py, adapters/herold.py, ...).

What lives here (source-agnostic, reused everywhere):
  - HTTP session + retry/backoff for plain `requests` traffic (non-WAF sites)
  - A Playwright page-fetch-with-retry helper, for sites behind bot-detection WAFs
  - Cookie-consent / overlay dismissal helpers (Playwright)
  - The Lead data model (one row = one company/location)
  - CSV resume + incremental flush (so a long-running scrape can be Ctrl+C'd
    and picked back up without re-fetching what's already saved)
  - Website "oldness" score + Impressum discovery/enrichment — these operate on
    ANY company website URL, regardless of which directory (WKO, Herold, ...)
    the lead came from.

What does NOT live here (goes in each adapters/<source>.py instead):
  - How that source's listing page is paginated (URL param? infinite scroll?
    a "load more" button? postback?)
  - Where on that source's detail page each field sits (regex/selectors)
  - Any source-specific quirks (cookie banners with unusual selectors, etc.)

Requirements:
    pip install requests beautifulsoup4 lxml playwright
    playwright install chromium
"""

import csv
import random
import time
from dataclasses import dataclass, asdict
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import Page
except ImportError:  # adapters that don't need a browser can still import this module
    Page = None  # type: ignore


# ── CONFIG (shared defaults — adapters may override per-instance) ──────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

REQUEST_DELAY_SECONDS = 5.5   # base politeness delay; adapters can tune per-target
TIMEOUT = 15
MAX_RETRIES = 2

IMPRESSUM_PATHS = ["/impressum", "/impressum/", "/de/impressum", "/kontakt/impressum"]
NO_UPDATE_SIGNS_YEAR_THRESHOLD = 2020  # copyright year older than this = +1 old point


def polite_sleep(base: float = None):
    """Base delay + small random jitter, so requests don't land on a fixed, detectable cadence."""
    time.sleep((base if base is not None else REQUEST_DELAY_SECONDS) + random.uniform(0, 1.5))


# ── Auto-generated CSV filenames: [Source][Bundesland][Branche].csv ────────
BUNDESLAND_ABBR = {
    "burgenland": "Bu",
    "kaernten": "Ka", "kärnten": "Ka",
    "niederoesterreich": "NOe", "niederösterreich": "NOe",
    "oberoesterreich": "OOe", "oberösterreich": "OOe",
    "salzburg": "Sa",
    "steiermark": "St",
    "tirol": "Ti",
    "vorarlberg": "Vo",
    "wien": "Wi",
}


def _abbr_region(region_slug: str) -> str:
    key = region_slug.lower().replace("-", "").replace(" ", "")
    if key in BUNDESLAND_ABBR:
        return BUNDESLAND_ABBR[key]
    # Unknown/foreign region slug: fall back to a short titlecase stub
    return region_slug.replace("-", " ").title().replace(" ", "")[:3]


def _abbr_branche(branche_slug: str) -> str:
    # Titlecase each hyphen-separated word, no truncation — keeps it human-readable
    # (e.g. "holzverarbeitung" -> "Holzverarbeitung", "gesundheit" -> "Gesundheit")
    return "".join(word.capitalize() for word in branche_slug.replace("_", "-").split("-"))


def default_csv_name(source_abbr: str, region_slug: str, branche_slug: str) -> str:
    """Builds e.g. 'WKOTiHolz.csv' or 'FirmABCBuBau.csv' from a source abbreviation
    plus the adapter's region/branche slugs."""
    return f"{source_abbr}{_abbr_region(region_slug)}{_abbr_branche(branche_slug)}.csv"


# ── DATA MODEL ──────────────────────────────────────────────────────────────
@dataclass
class Lead:
    """One row = one company/location. `source` + `source_url` identify where it came from,
    so rows from different adapters can be merged/deduped later without collisions."""
    name: str = ""
    plz: str = ""
    address: str = ""
    geschaeftsfuehrer: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    source: str = ""          # e.g. "wko", "herold"
    source_url: str = ""      # the detail-page URL this row was scraped from
    impressum_url: str = ""
    old_score: int = -1       # -1 = not evaluated (no website found)
    old_reasons: str = ""


# ── Generic `requests`-based fetch with retry (for non-WAF-protected sites) ─
def get_with_retry(url: str, session: requests.Session = None) -> requests.Response:
    """Plain requests GET with retry+backoff. Use for sites that don't fingerprint bots."""
    sess = session or SESSION
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = sess.get(url, timeout=TIMEOUT)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", REQUEST_DELAY_SECONDS))
                print(f"    [429] rate-limited, waiting {wait}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                time.sleep(REQUEST_DELAY_SECONDS)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exc = e
            time.sleep(REQUEST_DELAY_SECONDS * attempt)
    raise last_exc or requests.RequestException(f"Failed after {MAX_RETRIES} attempts: {url}")


# ── Generic Playwright fetch with retry (for WAF-protected sites) ──────────
def playwright_get_soup(page: "Page", url: str, delay: float = None) -> BeautifulSoup:
    """Navigate a real (headless or headed) browser page to url, return parsed HTML.
    Use for sites that block plain `requests` traffic via bot-detection."""
    d = delay if delay is not None else REQUEST_DELAY_SECONDS
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)  # let any JS-based content settle
            if resp and resp.status == 429:
                print(f"    [429] rate-limited, waiting {d:.0f}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(d)
                continue
            return BeautifulSoup(page.content(), "lxml")
        except Exception as e:
            last_exc = e
            time.sleep(d * attempt)
    raise last_exc or RuntimeError(f"Failed after {MAX_RETRIES} attempts: {url}")


def launch_stealth_browser(pw, headless: bool = True, locale: str = "de-AT"):
    """Launch a Chromium browser+context with basic anti-headless-detection measures.
    Returns (browser, context, page)."""
    browser = pw.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        user_agent=HEADERS["User-Agent"],
        locale=locale,
        viewport={"width": 1366, "height": 900},
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    page = context.new_page()
    return browser, context, page


# ── Cookie-consent / overlay removal (Playwright) ───────────────────────────
def dismiss_cookie_overlay(page: "Page") -> None:
    """Click through a cookie-consent banner if one is blocking interaction.
    Covers common consent-tool button texts/selectors; extend per-adapter if a
    site uses something unusual."""
    for selector in [
        'button:has-text("Akzeptieren")',
        'button:has-text("Accept")',
        'button:has-text("Alle akzeptieren")',
        'button:has-text("Okay")',
        'button:has-text("OK")',
        'button:has-text("Einverstanden")',
        '[id*="consent"] button',
        '[class*="consent"] button',
        '[aria-label*="close"]',
        '[data-testid*="close"]',
        '#cmp-wrapper button',
        '#cmp-root button',
        '#onetrust-close-btn-container button',
    ]:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                locator.click(timeout=3000)
                page.wait_for_timeout(1000)
                return
        except Exception:
            continue


def remove_overlays(page: "Page") -> None:
    """Hard-remove known overlay/backdrop elements that survive a normal click-dismiss."""
    page.evaluate("""
    () => {
      const roots = document.querySelectorAll('#cmp-root, #cmp-backdrop, .cmp-backdrop, .fc-dialog-container, .fc-dialog-overlay');
      roots.forEach(el => { el.remove(); });
      document.body.style.overflow='auto';
      document.documentElement.style.overflow='auto';
      const style = document.createElement('style');
      style.textContent = '#cmp-root, #cmp-backdrop, .cmp-backdrop, .fc-dialog-container, .fc-dialog-overlay { display: none !important; visibility: hidden !important; pointer-events: none !important; }';
      document.head.appendChild(style);
    }
    """)


# ── Website oldness score + Impressum discovery/enrichment (source-agnostic) ─
def find_impressum(website: str) -> str:
    parsed = urlparse(website)
    base = f"{parsed.scheme}://{parsed.netloc}"

    for path in IMPRESSUM_PATHS:
        candidate = base + path
        try:
            resp = SESSION.get(candidate, timeout=TIMEOUT, allow_redirects=True)
            if resp.status_code == 200 and "impressum" in resp.text.lower():
                return candidate
        except requests.RequestException:
            continue

    try:
        resp = get_with_retry(base)
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.find_all("a", href=True):
            if "impressum" in a["href"].lower() or "impressum" in a.get_text(strip=True).lower():
                return urljoin(base, a["href"])
    except requests.RequestException:
        pass

    return ""


def impressum_enrich(lead: Lead) -> None:
    """Fetch the lead's impressum page and fill in missing phone/email.
    If the impressum value differs from what we already have, both are kept, separated by ' // '."""
    if not lead.impressum_url:
        return

    try:
        resp = SESSION.get(lead.impressum_url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return
        soup = BeautifulSoup(resp.text, "lxml")
        text = soup.get_text(" ", strip=True)
    except requests.RequestException:
        return

    import re
    phone_match = re.search(r"(\+43[\s\d/\-]{6,20}|0\d{2,4}[\s/\-]\d{3,10})", text)
    impressum_phone = phone_match.group(1).strip() if phone_match else ""
    if impressum_phone:
        if not lead.phone:
            lead.phone = impressum_phone
        elif impressum_phone not in lead.phone and lead.phone not in impressum_phone:
            lead.phone = lead.phone + " // " + impressum_phone

    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    impressum_email = email_match.group(0) if email_match else ""
    if impressum_email:
        if not lead.email:
            lead.email = impressum_email
        elif impressum_email not in lead.email and lead.email not in impressum_email:
            lead.email = lead.email + " // " + impressum_email


def old_score_for_website(website: str) -> tuple:
    """Returns (score 0-5, reasons string). Higher score = older-looking site.
    Works for any company website, independent of which directory it came from."""
    score = 0
    reasons = []

    try:
        resp = SESSION.get(website, timeout=TIMEOUT)
        html = resp.text
        html_lower = html.lower()
    except requests.RequestException as e:
        return -1, f"unreachable ({e.__class__.__name__})"

    if not website.startswith("https"):
        score += 1
        reasons.append("no HTTPS")

    if "viewport" not in html_lower:
        score += 1
        reasons.append("no mobile viewport tag")

    import re
    years = re.findall(r"(?:©|copyright)\s*(\d{4})", html, re.IGNORECASE)
    if years:
        newest_year = max(int(y) for y in years)
        if newest_year < NO_UPDATE_SIGNS_YEAR_THRESHOLD:
            score += 1
            reasons.append(f"footer copyright year {newest_year}")

    cms_signatures = ["wp-content", "wix.com", "squarespace", "webflow", "cdn.shopify",
                       "typo3", 'generator" content="wordpress']
    if not any(sig in html_lower for sig in cms_signatures):
        score += 1
        reasons.append("no modern CMS fingerprint")

    table_count = html_lower.count("<table")
    if table_count >= 3 and "flex" not in html_lower and "grid" not in html_lower:
        score += 1
        reasons.append(f"table-heavy layout ({table_count} tables, no flex/grid)")

    return score, "; ".join(reasons) if reasons else "looks reasonably modern"


def enrich_website(lead: Lead) -> None:
    """Convenience wrapper: run impressum discovery + enrichment + oldness score
    for a lead that has a website. Call this from any adapter after parsing a detail page."""
    if not lead.website:
        return
    lead.impressum_url = find_impressum(lead.website)
    time.sleep(1.0)
    impressum_enrich(lead)
    lead.old_score, lead.old_reasons = old_score_for_website(lead.website)
    time.sleep(1.0)


# ── CSV resume + incremental flush ──────────────────────────────────────────
def load_existing(out_csv: str, key_field: str = "source_url"):
    """Load previously-saved rows so a run can resume without re-fetching them.
    Returns (existing_rows: list[dict], completed_keys: set[str])."""
    existing_rows = []
    completed_keys = set()
    try:
        with open(out_csv, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing_rows = list(reader)
            for row in existing_rows:
                k = row.get(key_field, "").strip()
                if k:
                    completed_keys.add(k)
        print(f"[i] Resuming: {len(completed_keys)} already completed, skipping them")
    except FileNotFoundError:
        pass
    return existing_rows, completed_keys


def flush_csv(out_csv: str, existing_rows: list, new_leads: list):
    """Write existing + newly-scraped rows to out_csv (overwrites with the combined set)."""
    fieldnames = list(Lead.__dataclass_fields__.keys())
    all_rows = existing_rows + [asdict(lead) for lead in new_leads]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)
