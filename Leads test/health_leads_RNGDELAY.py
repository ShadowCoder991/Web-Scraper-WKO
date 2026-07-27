#!/usr/bin/env python3
"""
Health-sector lead scraper (Austria).

Pipeline:
  1. Pull company listings from WKO Firmen A-Z for a given Bundesland + Branche.
  2. Visit each detail page -> extract name, address, Geschäftsführer/Inhaber, phone, email, website.
  3. Cross-check against FirmenABC by name search -> fill gaps / confirm GF name.
  4. If a website URL exists: find the Impressum page, extract contact data,
     and compute an "oldness score" (0-5, higher = more outdated).
  5. Write everything to a CSV.

Requirements:
    pip install requests beautifulsoup4 lxml playwright
    playwright install chromium

Notes:
  - WKO sits behind a bot-detection WAF that blocks plain `requests` traffic
    (different TLS fingerprint / no JS / missing browser headers) even though
    manual browsing works fine and no IP ban is in effect. So WKO listing +
    detail pages are fetched with a real headless Chromium via Playwright.
    Company websites / FirmenABC are usually less defended, so those still use
    plain `requests` — if FirmenABC also starts blocking, switch it to
    Playwright the same way.
  - WKO's markup changes occasionally. If extraction comes back empty, run with
    --debug on a single company and inspect the printed field values, then
    adjust the regex patterns below.
  - Be a polite scraper: REQUEST_DELAY_SECONDS between requests, one browser
    session reused for the whole run (mimics a real visitor browsing around),
    and respect robots.txt manually (WKO's directory pages are public and
    meant to be browsed/indexed).
"""

import argparse
import csv
import random
import re
import time
import sys
from dataclasses import dataclass, field, asdict
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page

# ── CONFIG ──────────────────────────────────────────────────────────────────
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
REQUEST_DELAY_SECONDS = 5.5   # base delay; +jitter below to avoid hitting WKO's refill window exactly
TIMEOUT = 15
MAX_RETRIES = 2

def polite_sleep():
    """Base delay + small random jitter, so we don't hit WKO's rate window at a fixed cadence."""
    time.sleep(REQUEST_DELAY_SECONDS + random.uniform(0, 1.5))


IMPRESSUM_PATHS = ["/impressum", "/impressum/", "/de/impressum", "/kontakt/impressum"]

NO_UPDATE_SIGNS_YEAR_THRESHOLD = 2020  # copyright year older than this = +1 old point

# ── DATA MODEL ──────────────────────────────────────────────────────────────
@dataclass
class Lead:
    name: str = ""
    plz: str = ""
    address: str = ""
    geschaeftsfuehrer: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    wko_url: str = ""
    firmenabc_confirmed: str = ""   # "yes"/"no"/"unchecked"
    impressum_url: str = ""
    old_score: int = -1             # -1 = not evaluated (no website found)
    old_reasons: str = ""


# ── Shared helper for non-WKO requests (FirmenABC, company websites) ────────
def get_with_retry(url: str) -> requests.Response:
    """Plain requests GET with retry+backoff, used for sites that aren't WAF-protected."""
    delay = REQUEST_DELAY_SECONDS
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = SESSION.get(url, timeout=TIMEOUT)
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
            time.sleep(delay * attempt)
    raise last_exc or requests.RequestException(f"Failed after {MAX_RETRIES} attempts: {url}")


def wko_get_soup(page: Page, url: str) -> BeautifulSoup:
    """Navigate a real (headless) browser page to url and return parsed HTML."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)  # let any JS-based content settle
            if resp and resp.status == 429:
                print(f"    [429] rate-limited, waiting {REQUEST_DELAY_SECONDS:.0f}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(REQUEST_DELAY_SECONDS)
                continue
            return BeautifulSoup(page.content(), "lxml")
        except Exception as e:
            last_exc = e
            time.sleep(REQUEST_DELAY_SECONDS * attempt)
    raise last_exc or RuntimeError(f"Failed after {MAX_RETRIES} attempts: {url}")


def dismiss_cookie_overlay(page: Page) -> None:
    """Dismiss the cookie consent overlay if it is blocking clicks."""
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


def _remove_overlays(page: Page) -> None:
    """Remove cookie-consent and other overlay elements that may block interactions."""
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



# ── STEP 2: WKO DETAIL PAGE ─────────────────────────────────────────────────
def parse_wko_detail(page: Page, url: str) -> Lead:
    lead = Lead(wko_url=url)
    try:
        soup = wko_get_soup(page, url)
    except Exception as e:
        print(f"[!] Failed to load detail page: {e}")
        return lead

    text = soup.get_text(" ", strip=True)

    # Name: usually the <h1>
    h1 = soup.find("h1")
    if h1:
        lead.name = h1.get_text(strip=True)

    # Geschäftsführer / Inhaber — appears as "Gewerberechtliche Geschäftsführung: NAME"
    # or "GeschäftsführerIn gewerberechtlich: NAME"
    gf_match = re.search(
        r"(?:Gewerberechtliche Geschäftsführung|GeschäftsführerIn gewerberechtlich|"
        r"Geschäftsführung|Inhaber(?:in)?)\s*:?\s*"
        r"([A-ZÄÖÜ][^\n·|]{2,60}?)"
        r"(?=\s*(?:GISA|Firmenwortlaut|Berufszweig|Behörde|Adresse|Gewerbewortlaut|"
        r"Datum|Seit\s\d|GLN|Firmenbuch|$))",
        text,
    )
    if gf_match:
        lead.geschaeftsfuehrer = gf_match.group(1).strip(" .,")

    # Address — "PLZ Ort, Straße" pattern e.g. "6060 Hall in Tirol, Thurnfeldgasse 3"
    addr_match = re.search(r"(\d{4})\s+([A-ZÄÖÜ][^\d,]{2,40},\s*[^\d]{2,40}\d{1,4}[a-zA-Z]?)", text)
    if addr_match:
        lead.plz = addr_match.group(1)
        lead.address = addr_match.group(2).strip()

    # Fallback: if PLZ wasn't captured, try to extract a 4-digit postal code from the address text
    if not lead.plz and lead.address:
        fb = re.match(r"(\d{4})\s+(.*)", lead.address)
        if fb:
            lead.plz = fb.group(1)
            lead.address = fb.group(2).strip()

    # Phone
    phone_match = re.search(r"(\+43[\s\d/\-]{6,20}|0\d{2,4}[\s/\-]\d{3,10})", text)
    if phone_match:
        lead.phone = phone_match.group(1).strip()

    # Email
    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    if email_match:
        lead.email = email_match.group(0)

    # Website — external link that isn't wko.at / google maps / social
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http") and not any(
            skip in href for skip in ["wko.at", "google.com/maps", "facebook.com",
                                        "instagram.com", "linkedin.com"]
        ):
            lead.website = href
            break

    return lead


# ── STEP 3: FIRMENABC CROSS-CHECK ───────────────────────────────────────────
def firmenabc_confirm(company_name: str, geschaeftsfuehrer_hint: str = "", debug: bool = False) -> str:
    """
    Searches FirmenABC for the company name and checks whether the GF name
    (if we have one) shows up on the result page too. Returns 'yes'/'no'/'unchecked'.
    """
    from urllib.parse import quote
    search_url = f"https://www.firmenabc.at/suche?q={quote(company_name)}"
    try:
        resp = get_with_retry(search_url)
    except requests.RequestException as e:
        if debug:
            print(f"    [firmenabc] request failed: {e}")
        return "unchecked"

    soup = BeautifulSoup(resp.text, "lxml")
    page_text = soup.get_text(" ", strip=True)

    if debug:
        print(f"    [firmenabc] url={search_url}")
        print(f"    [firmenabc] page_text[:200]={page_text[:200]!r}")

    first_word = company_name.split()[0].strip(",.") if company_name.split() else ""
    if not first_word or first_word not in page_text:
        return "no"

    if geschaeftsfuehrer_hint:
        surname = geschaeftsfuehrer_hint.split()[-1].strip(",.") if geschaeftsfuehrer_hint.split() else ""
        if surname and surname in page_text:
            return "yes"
        return "unchecked"
    return "unchecked"


# ── STEP 4: WEBSITE OLDNESS SCORE ───────────────────────────────────────────
def find_impressum(website: str) -> str:
    parsed = urlparse(website)
    base = f"{parsed.scheme}://{parsed.netloc}"

    # Try common paths first
    for path in IMPRESSUM_PATHS:
        candidate = base + path
        try:
            resp = SESSION.get(candidate, timeout=TIMEOUT, allow_redirects=True)
            if resp.status_code == 200 and "impressum" in resp.text.lower():
                return candidate
        except requests.RequestException:
            continue

    # Fall back: scan homepage for a link containing "impressum"
    try:
        resp = get_with_retry(base)
        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.find_all("a", href=True):
            if "impressum" in a["href"].lower() or "impressum" in a.get_text(strip=True).lower():
                return urljoin(base, a["href"])
    except requests.RequestException:
        pass

    return ""


# ── STEP 4b: IMPRESSUM ENRICHMENT ──────────────────────────────────
def impressum_enrich(lead: Lead, page: Page) -> None:
    """Fetch the company impressum page and fill in missing phone/email data.
    If WKO data differs from impressum data, both values are kept separated by //."""
    impressum_url = lead.impressum_url
    if not impressum_url:
        return

    try:
        resp = SESSION.get(impressum_url, timeout=TIMEOUT)
        if resp.status_code != 200:
            return
        soup = BeautifulSoup(resp.text, "lxml")
        text = soup.get_text(" ", strip=True)
    except requests.RequestException:
        return

    # Extract phone from impressum
    impressum_phone = ""
    phone_match = re.search(r"(\+43[\s\d/\-]{6,20}|0\d{2,4}[\s/\-]\d{3,10})", text)
    if phone_match:
        impressum_phone = phone_match.group(1).strip()

    # Merge phone: impressum fills WKO gap, or appends if different
    if impressum_phone:
        if not lead.phone:
            lead.phone = impressum_phone
        elif impressum_phone not in lead.phone and lead.phone not in impressum_phone:
            lead.phone = lead.phone + " // " + impressum_phone

    # Extract email from impressum
    impressum_email = ""
    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    if email_match:
        impressum_email = email_match.group(0)

    # Merge email same way
    if impressum_email:
        if not lead.email:
            lead.email = impressum_email
        elif impressum_email not in lead.email and lead.email not in impressum_email:
            lead.email = lead.email + " // " + impressum_email


def old_score_for_website(website: str) -> tuple[int, str]:
    """Returns (score 0-5, comma-separated reasons). Higher score = older-looking site."""
    score = 0
    reasons = []

    try:
        resp = SESSION.get(website, timeout=TIMEOUT)
        html = resp.text
        html_lower = html.lower()
    except requests.RequestException as e:
        return -1, f"unreachable ({e.__class__.__name__})"

    # 1. No HTTPS
    if not website.startswith("https"):
        score += 1
        reasons.append("no HTTPS")

    # 2. No viewport meta -> not mobile-optimized
    if "viewport" not in html_lower:
        score += 1
        reasons.append("no mobile viewport tag")

    # 3. Old copyright year in footer
    years = re.findall(r"(?:©|copyright)\s*(\d{4})", html, re.IGNORECASE)
    if years:
        newest_year = max(int(y) for y in years)
        if newest_year < NO_UPDATE_SIGNS_YEAR_THRESHOLD:
            score += 1
            reasons.append(f"footer copyright year {newest_year}")

    # 4. No modern CMS fingerprint (WordPress/Wix/Squarespace/Webflow/Shopify)
    cms_signatures = ["wp-content", "wix.com", "squarespace", "webflow", "cdn.shopify",
                       "typo3", "generator\" content=\"wordpress"]
    if not any(sig in html_lower for sig in cms_signatures):
        score += 1
        reasons.append("no modern CMS fingerprint")

    # 5. Table-based layout (old-school), heuristic: many <table> tags, few CSS grid/flex hints
    table_count = html_lower.count("<table")
    if table_count >= 3 and "flex" not in html_lower and "grid" not in html_lower:
        score += 1
        reasons.append(f"table-heavy layout ({table_count} tables, no flex/grid)")

    return score, "; ".join(reasons) if reasons else "looks reasonably modern"


# ── MAIN PIPELINE ────────────────────────────────────────────────────────────
def run(branche_slug: str, region_slug: str, limit: int, out_csv: str, debug: bool = False, headless: bool = True):
    # Resume: load already-completed URLs and rows from existing CSV
    completed_urls = set()
    existing_rows = []
    try:
        with open(out_csv, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing_rows = list(reader)
            for row in existing_rows:
                u = row.get("wko_url", "").strip()
                if u:
                    completed_urls.add(u)
        print(f"[i] Resuming: {len(completed_urls)} already completed, skipping them")
    except FileNotFoundError:
        pass

    leads = []
    count = 0

    def flush_csv():
        fieldnames = list(Lead.__dataclass_fields__.keys())
        all_rows = existing_rows + [asdict(lead) for lead in leads]
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="de-AT",
            viewport={"width": 1366, "height": 900},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()

        print(f"[i] Collecting listing URLs for {branche_slug}/{region_slug}...")
        base = f"https://firmen.wko.at/{branche_slug}/{region_slug}/"

        page.goto(base, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        page.wait_for_timeout(8000)
        _remove_overlays(page)
        page.wait_for_timeout(1000)

        seen_urls = set(completed_urls)
        batch_size = 100
        max_clicks_per_batch = 50

        while count < limit:
            batch = []
            clicks = 0
            while len(batch) < batch_size:
                soup = BeautifulSoup(page.content(), "lxml")
                links = soup.select("h3 a[href*='firmaid=']") or soup.select("a[href*='firmaid=']")
                new_on_page = 0
                for a in links:
                    href = urljoin(base, a.get("href", ""))
                    if href not in seen_urls:
                        seen_urls.add(href)
                        batch.append(href)
                        new_on_page += 1
                        if len(batch) >= batch_size:
                            break

                print(f"    [dbg] Page scan: {new_on_page} new URLs ({len(batch)} in batch so far)")

                if len(batch) >= batch_size:
                    break

                if clicks >= max_clicks_per_batch:
                    print(f"[i] Reached max clicks ({max_clicks_per_batch}) for this batch.")
                    break

                btn = page.locator('input[value="Mehr laden"]').first
                if btn.count() == 0:
                    print("[i] 'Mehr laden' button not found, stopping.")
                    break

                try:
                    btn.scroll_into_view_if_needed()
                    page.wait_for_timeout(300)
                    btn.click(timeout=30000, force=True)
                    _remove_overlays(page)
                    page.wait_for_timeout(8000)
                    clicks += 1
                    print(f"    [dbg] Clicked 'Mehr laden' ({clicks}/{max_clicks_per_batch})")
                except Exception as e:
                    print(f"[i] Click failed: {e}")
                    break

            if not batch:
                print("[i] No more URLs to collect.")
                break

            print(f"[i] Collected batch of {len(batch)} URLs (clicked 'Mehr laden' {clicks} times)")

            for detail_url in batch:
                if count >= limit:
                    break
                if detail_url in completed_urls:
                    print(f"[i] Skipping already completed: {detail_url}")
                    continue

                print(f"[{count+1}/{limit}] Fetching {detail_url}")
                lead = parse_wko_detail(page, detail_url)
                polite_sleep()

                if debug:
                    print(f"    name={lead.name!r} gf={lead.geschaeftsfuehrer!r} "
                          f"addr={lead.address!r} phone={lead.phone!r} email={lead.email!r} "
                          f"website={lead.website!r}")

                if lead.website:
                    lead.impressum_url = find_impressum(lead.website)
                    time.sleep(1.0)
                    impressum_enrich(lead, page)
                    lead.old_score, lead.old_reasons = old_score_for_website(lead.website)
                    time.sleep(1.0)

                leads.append(lead)
                count += 1
                flush_csv()
                print(f"    [i] CSV updated: {len(existing_rows) + len(leads)} total rows")

            if count < limit:
                try:
                    response = input(f"\n[i] Processed {count} leads. Fetch another batch of {batch_size}? (y/n): ").strip().lower()
                    if response != 'y':
                        print("[i] Stopping by user request.")
                        break

                    page.goto(base, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
                    page.wait_for_timeout(8000)
                    _remove_overlays(page)
                    page.wait_for_timeout(1000)
                except EOFError:
                    print("\n[i] Non-interactive mode, stopping.")
                    break

        browser.close()

    flush_csv()
    print(f"\nDone. {len(existing_rows) + len(leads)} leads written to {out_csv}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scrape health-sector leads from WKO Firmen A-Z.")
    ap.add_argument("--branche", default="gesundheit", help="WKO branche slug, e.g. gesundheit")
    ap.add_argument("--region", default="tirol", help="WKO region slug, e.g. tirol")
    ap.add_argument("--limit", type=int, default=100000, help="Max number of companies to process")
    ap.add_argument("--out", default="leads.csv", help="Output CSV path")
    ap.add_argument("--debug", action="store_true", help="Print extracted fields per company")
    ap.add_argument("--headed", action="store_true",
                     help="Run with a visible browser window (helps bypass headless-detection WAFs)")
    args = ap.parse_args()

    run(args.branche, args.region, args.limit, args.out, args.debug, headless=not args.headed)
