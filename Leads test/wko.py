#!/usr/bin/env python3
"""
WKO Firmen A-Z adapter.

Source-specific quirks handled here (nowhere else):
  - WKO sits behind a bot-detection WAF that blocks plain `requests` traffic
    (different TLS fingerprint / no JS / missing browser headers) even though
    manual browsing works fine. So listing + detail pages go through a real
    (stealth-flagged) headless Chromium via Playwright — see scraper_core.playwright_get_soup.
  - The listing page has NO url-param pagination and NO postback — it's a
    "Mehr laden" (load more) button that appends results via JS. So we click
    it repeatedly and re-scan the page's HTML for new detail links each time.
  - Detail-page field extraction (name/address/PLZ/phone/email/GF) is regex
    tuned to WKO's specific markup — if WKO changes their template, only this
    file needs updating, not scraper_core.py or other adapters.

Requirements:
    pip install requests beautifulsoup4 lxml playwright
    playwright install chromium

Usage:
    python -m adapters.wko --branche gesundheit --region tirol --out leads_wko.csv --debug
"""

import argparse
import re
import sys
import os
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper_core as core
from scraper_core import Lead

REQUEST_DELAY_SECONDS = 5.5   # WKO-specific pacing; tune here without touching core defaults
TIMEOUT = 15


# ── DETAIL PAGE PARSING (WKO-specific markup) ───────────────────────────────
def parse_detail(page: Page, url: str) -> Lead:
    lead = Lead(source="wko", source_url=url)
    try:
        soup = core.playwright_get_soup(page, url, delay=REQUEST_DELAY_SECONDS)
    except Exception as e:
        print(f"[!] Failed to load detail page: {e}")
        return lead

    text = soup.get_text(" ", strip=True)

    # Name: usually the <h1>
    h1 = soup.find("h1")
    if h1:
        lead.name = h1.get_text(strip=True)

    # Geschäftsführer / Inhaber
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
    elif lead.address:
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

    # Website — external link that isn't wko.at / maps / social
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http") and not any(
            skip in href for skip in ["wko.at", "google.com/maps", "facebook.com",
                                        "instagram.com", "linkedin.com"]
        ):
            lead.website = href
            break

    return lead


# ── LISTING (WKO-specific: 'Mehr laden' button, no URL pagination) ─────────
def collect_batch(page: Page, base_url: str, seen_urls: set, batch_size: int = 100,
                   max_clicks: int = 50) -> list:
    """Scans the current listing page for new detail-page URLs, clicking 'Mehr laden'
    as needed until `batch_size` new URLs are collected or no more can be loaded."""
    batch = []
    clicks = 0
    while len(batch) < batch_size:
        soup = BeautifulSoup(page.content(), "lxml")
        links = soup.select("h3 a[href*='firmaid=']") or soup.select("a[href*='firmaid=']")
        new_on_page = 0
        for a in links:
            href = urljoin(base_url, a.get("href", ""))
            if href not in seen_urls:
                seen_urls.add(href)
                batch.append(href)
                new_on_page += 1
                if len(batch) >= batch_size:
                    break

        print(f"    [dbg] Page scan: {new_on_page} new URLs ({len(batch)} in batch so far)")

        if len(batch) >= batch_size:
            break
        if clicks >= max_clicks:
            print(f"[i] Reached max clicks ({max_clicks}) for this batch.")
            break

        btn = page.locator('input[value="Mehr laden"]').first
        if btn.count() == 0:
            print("[i] 'Mehr laden' button not found, stopping.")
            break

        try:
            btn.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            btn.click(timeout=30000, force=True)
            core.remove_overlays(page)
            page.wait_for_timeout(8000)
            clicks += 1
            print(f"    [dbg] Clicked 'Mehr laden' ({clicks}/{max_clicks})")
        except Exception as e:
            print(f"[i] Click failed: {e}")
            break

    if batch:
        print(f"[i] Collected batch of {len(batch)} URLs (clicked 'Mehr laden' {clicks} times)")
    return batch


# ── MAIN PIPELINE ────────────────────────────────────────────────────────────
def run(branche_slug: str, region_slug: str, limit: int, out_csv: str,
        debug: bool = False, headless: bool = True, batch_size: int = 100, interactive: bool = True):
    existing_rows, completed_urls = core.load_existing(out_csv, key_field="source_url")
    leads = []
    count = 0
    base = f"https://firmen.wko.at/{branche_slug}/{region_slug}/"

    with sync_playwright() as pw:
        browser, context, page = core.launch_stealth_browser(pw, headless=headless)

        print(f"[i] Collecting listing URLs for {branche_slug}/{region_slug}...")
        page.goto(base, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
        page.wait_for_timeout(8000)
        core.remove_overlays(page)
        page.wait_for_timeout(1000)

        seen_urls = set(completed_urls)

        while count < limit:
            batch = collect_batch(page, base, seen_urls, batch_size=batch_size)
            if not batch:
                print("[i] No more URLs to collect.")
                break

            for detail_url in batch:
                if count >= limit:
                    break
                if detail_url in completed_urls:
                    print(f"[i] Skipping already completed: {detail_url}")
                    continue

                print(f"[{count+1}/{limit}] Fetching {detail_url}")
                lead = parse_detail(page, detail_url)
                core.polite_sleep(REQUEST_DELAY_SECONDS)

                if debug:
                    print(f"    name={lead.name!r} gf={lead.geschaeftsfuehrer!r} "
                          f"addr={lead.address!r} phone={lead.phone!r} email={lead.email!r} "
                          f"website={lead.website!r}")

                core.enrich_website(lead)

                leads.append(lead)
                count += 1
                core.flush_csv(out_csv, existing_rows, leads)
                print(f"    [i] CSV updated: {len(existing_rows) + len(leads)} total rows")

            if count < limit and interactive:
                try:
                    response = input(f"\n[i] Processed {count} leads. Fetch another batch of {batch_size}? (y/n): ").strip().lower()
                    if response != 'y':
                        print("[i] Stopping by user request.")
                        break
                    page.goto(base, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
                    page.wait_for_timeout(8000)
                    core.remove_overlays(page)
                    page.wait_for_timeout(1000)
                except EOFError:
                    print("\n[i] Non-interactive mode, stopping.")
                    break
            elif count < limit and not interactive:
                page.goto(base, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
                page.wait_for_timeout(8000)
                core.remove_overlays(page)
                page.wait_for_timeout(1000)

        browser.close()

    core.flush_csv(out_csv, existing_rows, leads)
    print(f"\nDone. {len(existing_rows) + len(leads)} leads written to {out_csv}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scrape leads from WKO Firmen A-Z.")
    ap.add_argument("--branche", default="gesundheit", help="WKO branche slug, e.g. gesundheit")
    ap.add_argument("--region", default="tirol", help="WKO region slug, e.g. tirol")
    ap.add_argument("--limit", type=int, default=100000, help="Max number of companies to process")
    ap.add_argument("--out", default=None,
                     help="Output CSV path. If omitted, auto-generated as e.g. WKOTiHolz.csv "
                          "from --region/--branche.")
    ap.add_argument("--debug", action="store_true", help="Print extracted fields per company")
    ap.add_argument("--headed", action="store_true", help="Run with a visible browser window")
    ap.add_argument("--batch-size", type=int, default=100, help="URLs to collect per 'Mehr laden' batch")
    ap.add_argument("--no-interactive", action="store_true",
                     help="Don't ask 'continue?' between batches — run straight through")
    args = ap.parse_args()

    out_csv = args.out or core.default_csv_name("WKO", args.region, args.branche)
    print(f"[i] Output file: {out_csv}")

    run(args.branche, args.region, args.limit, out_csv, args.debug,
        headless=not args.headed, batch_size=args.batch_size, interactive=not args.no_interactive)
