#!/usr/bin/env python3
"""
FirmenABC.at adapter.

Source-specific quirks handled here (nowhere else):
  - Clean URL-based pagination:
        https://www.firmenabc.at/firmen/{region_code}/{branche_slug}_{code}
    `branche_slug` and `region_slug` are auto-resolved to codes via `FirmenABC_index.txt`
    if it's in the working directory. You can still pass the full slug+code explicitly
    (e.g. `gesundheitswesen_CXm`).
  - Name + street/PLZ/Ort are already shown on the LISTING page, so we grab those
    without a detail-page visit. Only phone/email/website require the detail page.
  - Detail pages show contact info as plain "T:", "M:", "W:" lines — easy to parse.
  - No Geschäftsführer/Inhaber field on FirmenABC (unlike WKO) — that column stays
    empty for this source. That's expected, not a bug: not every source has every field.
  - FirmenABC now blocks plain `requests` with 429 on the listing pages, so this
    adapter uses Playwright via `scraper_core.playwright_get_soup` for both
    listing and detail fetches.

Requirements:
    pip install requests beautifulsoup4 lxml playwright
    playwright install chromium

Usage:
    python -m adapters.firmenabc1 --branche gesundheitswesen --region tirol --limit 5 --debug
"""

import argparse
import random
import re
import sys
import os
import time
from urllib.parse import urljoin

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper_core as core
from scraper_core import Lead

REQUEST_DELAY_SECONDS = 7.0
TIMEOUT = 15


def parse_firmenabc_index(index_path: str) -> tuple:
    """Parse FirmenABC_index.txt and return two mappings:
    branche_slug -> code, and region code -> normalized region slug."""
    branche_map = {}
    region_map = {}

    region_abbr = {
        "burgenland": "bgld",
        "kärnten": "ktn",
        "niederösterreich": "noe",
        "oberösterreich": "ooe",
        "salzburg": "sbg",
        "steiermark": "stmk",
        "tirol": "tirol",
        "vorarlberg": "vbg",
        "wien": "wi",
    }

    url_re = re.compile(r"https://www\.firmenabc\.at/firmen/([a-z]{2,})/([a-z0-9\-]+)_([A-Za-z0-9]+)")

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if not line.startswith("https://"):
                    continue

                m = url_re.search(line)
                if not m:
                    continue

                region_code, branche_slug, code = m.groups()

                if branche_slug not in branche_map:
                    branche_map[branche_slug] = code

                region_slug = next((k for k, v in region_abbr.items() if v == region_code), region_code)
                if region_code not in region_map:
                    region_map[region_code] = region_slug

    except FileNotFoundError:
        print(f"[i] Index file not found: {index_path}. Will fall back to --branche with explicit code.")

    return branche_map, region_map


BRANCHE_CODE_MAP, REGION_CODE_MAP = parse_firmenabc_index("FirmenABC_index.txt")


def resolve_branche(branche_input: str) -> str:
    branche_input = branche_input.lower().strip()
    if "_" in branche_input:
        branche, code = branche_input.rsplit("_", 1)
        return f"{branche}_{code.upper()}"
    if branche_input in BRANCHE_CODE_MAP:
        return f"{branche_input}_{BRANCHE_CODE_MAP[branche_input]}"
    return branche_input


def resolve_region(region_input: str) -> str:
    region_input = region_input.lower().strip().replace("-", "")
    if region_input in REGION_CODE_MAP.values():
        return next((k for k, v in REGION_CODE_MAP.items() if v == region_input), region_input)
    abbr = {
        "burgenland": "bgld",
        "kärnten": "ktn",
        "niederösterreich": "noe",
        "oberösterreich": "ooe",
        "salzburg": "sbg",
        "steiermark": "stmk",
        "tirol": "tirol",
        "vorarlberg": "vbg",
        "wien": "wi",
    }
    return abbr.get(region_input, region_input)


def listing_url(region_code: str, branche_slug_with_code: str, page_num: int) -> str:
    base = f"https://www.firmenabc.at/firmen/{region_code}/{branche_slug_with_code}"
    return base if page_num == 1 else f"{base}/{page_num}"


# ── LISTING (name + address already present here) ───────────────────────────
def parse_listing_page(html: str, base_url: str = "") -> list:
    """Returns a list of partially-filled Lead objects (name, plz, address, source_url)
    found on one listing page. Phone/email/website still need the detail page."""
    soup = BeautifulSoup(html, "lxml")
    leads = []

    detail_link_re = re.compile(r"^https://www\.firmenabc\.at/[a-z0-9\-]+_[A-Za-z0-9]+$")

    for heading in soup.find_all(["h2", "h3"]):
        a = heading.find("a", href=True)
        if not a:
            continue
        href = urljoin(base_url, a["href"])
        if not detail_link_re.match(href):
            continue

        name = a.get_text(strip=True)
        lead = Lead(name=name, source="firmenabc", source_url=href)

        addr_text = ""
        sib = heading.find_next_sibling()
        hops = 0
        while sib and hops < 6:
            text = sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else str(sib).strip()
            if text and re.search(r"\d{4}", text):
                addr_text = text
                break
            sib = sib.find_next_sibling()
            hops += 1

        m = re.search(r"(.+?)\s+(\d{4})\s+([A-ZÄÖÜ].+)", addr_text)
        if m:
            lead.address = m.group(1).strip() + ", " + m.group(3).strip()
            lead.plz = m.group(2)

        leads.append(lead)

    return leads


def collect_all_listing_leads(page, region_code: str, branche_slug_with_code: str,
                               max_pages: int = 2000, debug: bool = False):
    """Yields Lead objects (name/address filled, phone/email/website still empty)
    by walking pages 1..N until a page has no new detail links."""
    page_num = 1
    while page_num <= max_pages:
        url = listing_url(region_code, branche_slug_with_code, page_num)
        try:
            soup = core.playwright_get_soup(page, url, delay=REQUEST_DELAY_SECONDS)
            html = str(soup)
        except Exception as e:
            print(f"[!] Failed to load listing page {page_num}: {e}")
            break

        leads = parse_listing_page(html, url)
        if not leads:
            print(f"[i] No results on page {page_num}, stopping pagination.")
            break

        if debug:
            print(f"    [dbg] page {page_num}: {len(leads)} entries")

        for lead in leads:
            yield lead

        page_num += 1
        time.sleep(REQUEST_DELAY_SECONDS + random.uniform(0, 2))


# ── DETAIL PAGE (phone / email / website / Handelnde Personen) ──────────────

# Known role labels used in the "Handelnde Personen" block.
_PERSON_ROLES = [
    "persönlich haftender Gesellschafter", "persönlich haftende Gesellschafterin",
    "Geschäftsführerin", "Geschäftsführer",
    "Gesellschafterin", "Gesellschafter",
    "Kommanditistin", "Kommanditist",
    "Komplementärin", "Komplementär",
    "Prokuristin", "Prokurist",
    "Inhaberin", "Inhaber",
    "Vorstand", "Aufsichtsrat",
    "Liquidatorin", "Liquidator",
    "leitender Angestellter", "leitende Angestellte",
    "Gesellschafter", "Geschäftsführer",
]
_ROLE_ALTERNATION = "|".join(_PERSON_ROLES)


def _normalize_site(url: str) -> str:
    url = url.strip().lower()
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    if url.startswith("https://"):
        url = "https://" + url[len("https://"):].lstrip("www.")
    if url.endswith("/"):
        url = url[:-1]
    return url


def _join_unique(values: list) -> str:
    seen = []
    for v in values:
        v = v.strip()
        if v and v not in seen:
            seen.append(v)
    return " // ".join(seen)


_STATUS_LINES = {"Privatperson", "Firma", "Anteil", "alleinvertretungsberechtigt",
                 "beschränkt vertretungsberechtigt", "vertretungsberechtigt", "beschränkt haftend"}

_BETEILIGUNGEN_RE = re.compile(r"^Beteiligungen von .+$", re.IGNORECASE)
_PERCENT_RE = re.compile(r"^\d{1,3},\d{2}\s*%?$")


def _line_is_role(line: str) -> bool:
    low = line.lower()
    if any(role.lower() in low for role in _PERSON_ROLES):
        return True
    for pat in ["persönlich haftender", "haftender gesellschafter", "geschäftsführer",
                "inhaber", "prokurist", "vorstand", "gesellschafter"]:
        if pat in low:
            return True
    return False


def _extract_personen(full_text: str) -> str:
    if "Handelnde Personen" not in full_text:
        return ""

    section = full_text.split("Handelnde Personen", 1)[1]
    for stop_marker in ["Alle Angaben erfolgen", "Bonitätsauskünfte", "Firmenbuchauszug",
                        "Inhalte des amtlichen", "Firmenbuch ist", "Kostenpflichtige Leistungen",
                        "Bewertungen", "Relevante Branchen", "Jobs", "Full-Service-Angebot",
                        "Angaben", "Gegenstand", "Sonstiges", "Wir haben"]:
        if stop_marker in section:
            section = section.split(stop_marker, 1)[0]
            break

    lines = [line.strip() for line in section.split("\n") if line.strip()]

    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _BETEILIGUNGEN_RE.match(line):
            result.append(f"Beteiligungen: {line}")
            i += 1
            continue
        if _line_is_role(line):
            name_parts = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt in _STATUS_LINES:
                    j += 1
                    continue
                if _PERCENT_RE.match(nxt):
                    j += 1
                    continue
                if nxt == "Anteil:":
                    j += 1
                    continue
                if _BETEILIGUNGEN_RE.match(nxt):
                    break
                if _line_is_role(nxt):
                    break
                if len(nxt) > 120 or nxt.startswith(("Alle ", "Bonit", "Firmenbuch", "Inhalte", "Kosten", "Mehr laden", "Jahresabschluss", "Geschäftsführung", "Prokura", "Kapital", "Bilanz", "Eigenkapital", "Unternehmensdaten", "Firmenhistorie", "Chronologische", "Aktenzeichen", "ANTEILE", "Bewertungen", "Relevante Branchen", "Jobs", "Full-Service")):
                    break
                name_parts.append(nxt)
                j += 1
            if name_parts:
                result.append(f"{line}: {' '.join(name_parts)}")
            i = j
        else:
            i += 1

    if result:
        return " // ".join(result)

    pattern = re.compile(
        rf"({'|'.join(_PERSON_ROLES)})\s*:?\s*(?:Frau|Herr)?\s*([A-ZÄÖÜa-zA-Zöäß][^\n]{{2,80}}?)"
        rf"(?=\s*\n?\s*(?:Privatperson|Firma|Anteil|alleinvertretungsberechtigt|"
        rf"{'|'.join(_PERSON_ROLES)}|$))"
    )
    pairs = [f"{role.strip()}: {name.strip(' ,')}" for role, name in pattern.findall(section)]
    if pairs:
        return " // ".join(dict.fromkeys(pairs))

    return ""


def enrich_detail(page, lead: Lead) -> None:
    try:
        soup = core.playwright_get_soup(page, lead.source_url, delay=REQUEST_DELAY_SECONDS)
        full_text = soup.get_text("\n", strip=True)
    except Exception as e:
        print(f"[!] Failed to load detail page: {e}")
        return

    hero_text = full_text.split("Ihr Unternehmen?")[0]

    phones = re.findall(r"(?:^|\n)T:\s*([+\d][\d /\-]{4,20})", hero_text)
    lead.phone = _join_unique(phones)

    emails = re.findall(r"(?:^|\n)M:\s*([\w.+-]+@[\w-]+\.[\w.-]+)", hero_text)
    lead.email = _join_unique(emails)

    junk_domains = {
        "firmenabc.at", "firmenabc.com", "business.safety.google", "site.adform.com",
        "cookiebot.com", "www.cookiebot.com", "facebook.com", "instagram.com",
        "linkedin.com", "youtube.com", "consentmanager.net", "usercentrics.eu",
        "privacy-mgmt.com", "global-privacy-control.com", "trustarc.com",
        "onetrust.com", "termly.io", "cookielaw.org", "quantcast.com",
        "googletagmanager.com", "google-analytics.com", "googlesyndication.com",
    }

    websites = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            continue
        domain = href.split("/")[2].lower()
        if domain.startswith("www."):
            domain = domain[4:]
        if any(jd in domain for jd in junk_domains):
            continue
        link_text = a.get_text(strip=True)
        if not link_text or link_text not in hero_text:
            continue
        norm = _normalize_site(href)
        if norm not in [_normalize_site(w) for w in websites]:
            websites.append(norm)
    lead.website = " // ".join(websites)

    lead.personen = _extract_personen(full_text)
def run(branche_slug: str, region_slug: str, limit: int, out_csv: str, debug: bool = False):
    existing_rows, completed_urls = core.load_existing(out_csv, key_field="source_url")
    leads = []
    count = 0

    branche_slug = resolve_branche(branche_slug)
    region_code = resolve_region(region_slug)

    with sync_playwright() as pw:
        browser, context, page = core.launch_stealth_browser(pw, headless=True)

        print(f"[i] Collecting listing entries for {branche_slug}/{region_code}...")
        for lead in collect_all_listing_leads(page, region_code, branche_slug, debug=debug):
            if count >= limit:
                break
            if lead.source_url in completed_urls:
                continue

            print(f"[{count+1}/{limit}] {lead.name}")
            enrich_detail(page, lead)
            time.sleep(REQUEST_DELAY_SECONDS + random.uniform(0, 2))

            if debug:
                print(f"    name={lead.name!r} addr={lead.address!r} plz={lead.plz!r} "
                      f"phone={lead.phone!r} email={lead.email!r} website={lead.website!r} "
                      f"personen={getattr(lead, 'personen', '')!r}")

            core.enrich_website(lead)

            leads.append(lead)
            count += 1
            core.flush_csv(out_csv, existing_rows, leads)

        browser.close()

    core.flush_csv(out_csv, existing_rows, leads)
    print(f"\nDone. {len(existing_rows) + len(leads)} leads written to {out_csv}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scrape leads from FirmenABC.at")
    ap.add_argument("--branche", required=True,
                     help="Branche slug WITH code (e.g. gesundheitswesen_CXm) or plain slug (e.g. gesundheitswesen); "
                          "index lookup is automatic if FirmenABC_index.txt is present")
    ap.add_argument("--region", default="tirol", help="FirmenABC region slug, e.g. tirol")
    ap.add_argument("--limit", type=int, default=100000, help="Max number of companies to process")
    ap.add_argument("--out", default=None,
                     help="Output CSV path. If omitted, auto-generated as e.g. FirmABCTiGesundheitswesen.csv")
    ap.add_argument("--debug", action="store_true", help="Print extracted fields per company")
    args = ap.parse_args()

    out_csv = args.out or core.default_csv_name("FirmABC", args.region, args.branche)
    print(f"[i] Output file: {out_csv}")

    run(args.branche, args.region, args.limit, out_csv, args.debug)
