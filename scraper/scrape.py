#!/usr/bin/env python3
"""
Flight offer scraper for Indian booking platforms.

Strategy: navigate to each platform's offers page with a real Chromium
browser (via Playwright), wait for JS to settle, then extract all visible
text.  Offer data is parsed from that text using bank-name + discount-amount
patterns — more resilient than CSS selectors that break whenever a site
redesigns.

Run locally:
    pip install playwright beautifulsoup4
    playwright install chromium
    python scraper/scrape.py
"""

import asyncio
import json
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

BANKS = [
    "HDFC", "SBI", "State Bank", "Axis", "ICICI", "Kotak",
    "Amex", "American Express", "IndusInd", "RBL",
    "IDFC First", "IDFC", "AU Bank", "AU Small Finance",
    "Yes Bank", "Federal Bank", "Bank of Baroda", "BOB",
    "Standard Chartered", "StanChart", "Canara Bank",
]

# Words that look like promo codes but aren't
_NOT_CODES = {
    "HDFC", "ICICI", "AXIS", "KOTAK", "AMEX", "INDUSIND", "IDFC",
    "EMI", "UPI", "RBL", "SBI", "BANK", "CARD", "FLAT", "FREE",
    "BOOK", "SAVE", "DEAL", "EASY", "BEST", "HOME", "FLIGHT",
    "HOTEL", "BUS", "TRAIN", "OFFER", "CODE", "PROMO",
}

PLATFORMS = [
    {
        "id": "MakeMyTrip",
        "urls": [
            "https://www.makemytrip.com/offers/flights-offers.html",
            "https://www.makemytrip.com/offers/bank-offers-on-flight.html",
        ],
    },
    {
        "id": "GoIbibo",
        "urls": [
            "https://www.goibibo.com/offers/",
            "https://www.goibibo.com/offers/flight-offers/",
        ],
    },
    {
        "id": "Yatra",
        "urls": [
            "https://www.yatra.com/offers/flight-offers.html",
            "https://www.yatra.com/online-flights/deals.html",
        ],
    },
    {
        "id": "Cleartrip",
        "urls": [
            "https://www.cleartrip.com/offers/",
            "https://www.cleartrip.com/offers/flights",
        ],
    },
    {
        "id": "EaseMyTrip",
        "urls": [
            "https://www.easemytrip.com/offers/flight-offers",
            "https://www.easemytrip.com/offers.html",
        ],
    },
    {
        "id": "Ixigo",
        "urls": [
            "https://www.ixigo.com/offers/",
            "https://www.ixigo.com/offers/flights",
        ],
    },
]

# ──────────────────────────────────────────────────────────────────────────────
# Text-based offer extraction helpers
# ──────────────────────────────────────────────────────────────────────────────

def _bank_in(text: str) -> str | None:
    for bank in BANKS:
        if re.search(r'\b' + re.escape(bank) + r'\b', text, re.IGNORECASE):
            return bank
    return None

def _discount(text: str) -> str | None:
    # Prefer rupee amount; fall back to percentage
    for pat in [
        r'(?:flat|upto|up\s+to|save|get)?\s*₹\s*([\d,]+)',
        r'(?:flat|upto|up\s+to|save|get)?\s*(\d{1,3})\s*%\s*(?:off|instant|cashback|discount)',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return f"₹{m.group(1)}" if '₹' in pat else f"{m.group(1)}% off"
    return None

def _promo_code(text: str) -> str | None:
    # Explicitly labelled code
    m = re.search(
        r'(?:use\s+code|coupon|promo\s+code|apply\s+code)[:\s]+([A-Z][A-Z0-9]{3,11})\b',
        text, re.IGNORECASE,
    )
    if m:
        code = m.group(1).upper()
        if code not in _NOT_CODES:
            return code
    # Standalone alphanumeric tokens that have both letters and digits
    for tok in re.findall(r'\b([A-Z][A-Z0-9]{4,11})\b', text):
        if tok not in _NOT_CODES and not tok.isalpha() and not tok.isdigit():
            return tok
    return None

def _validity(text: str) -> str | None:
    patterns = [
        r'valid\s+(?:till|until|through)[:\s]+([^\n.]{3,30})',
        r'expires?\s*(?:on)?[:\s]+([^\n.]{3,30})',
        r'offer\s+ends?\s*[:\s]+([^\n.]{3,30})',
        r'(\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d{2,4})',
        r'(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:30]
    return None

def _min_booking(text: str) -> str | None:
    m = re.search(
        r'(?:min(?:imum)?\s+(?:booking|txn|transaction|purchase|order|spend)[:\s]+₹?\s*([\d,]+)'
        r'|on\s+(?:a\s+)?(?:minimum\s+)?(?:booking|transaction)\s+of\s+₹?\s*([\d,]+))',
        text, re.IGNORECASE,
    )
    if m:
        amt = m.group(1) or m.group(2)
        return f"₹{amt}"
    return None

def _is_emi(text: str) -> bool:
    return bool(re.search(
        r'\bEMI\b|no[\s-]cost\s+EMI|easy\s+instalment|equated\s+monthly',
        text, re.IGNORECASE,
    ))

def _clean(raw: str) -> str:
    text = re.sub(r'\s+', ' ', raw).strip()
    if len(text) > 85:
        text = text[:82].rsplit(' ', 1)[0] + '…'
    return text

def parse_offers(page_text: str) -> list[dict]:
    """
    Split page text into chunks; extract one offer per chunk that contains
    both a bank name and a discount amount.
    """
    offers, seen = [], set()

    # Split on blank lines or transitions from a digit/period to an uppercase letter
    chunks = re.split(r'\n{2,}|(?<=[\d.])\n(?=[A-Z])', page_text)

    for chunk in chunks:
        chunk = chunk.strip()
        if len(chunk) < 15:
            continue

        bank     = _bank_in(chunk)
        discount = _discount(chunk)
        if not bank or not discount:
            continue

        key = (bank.lower(), discount.lower())
        if key in seen:
            continue
        seen.add(key)

        offers.append({
            "bank":         bank,
            "card":         "All cards",
            "offer":        _clean(chunk),
            "max_discount": discount,
            "promo_code":   _promo_code(chunk),
            "valid_until":  _validity(chunk),
            "min_booking":  _min_booking(chunk),
            "offer_type":   "EMI" if _is_emi(chunk) else "Non-EMI",
        })

        if len(offers) >= 25:   # cap per platform to avoid noise
            break

    return offers

# ──────────────────────────────────────────────────────────────────────────────
# Playwright helpers
# ──────────────────────────────────────────────────────────────────────────────

# CSS selectors to try for offer card containers (tried in order)
_CARD_SELECTORS = [
    "[class*='offer-card']",
    "[class*='offerCard']",
    "[class*='offer_card']",
    "[class*='bank-offer']",
    "[class*='bankOffer']",
    "[class*='deal-card']",
    "[class*='dealCard']",
    "[class*='promo-card']",
    "article",
]

async def _page_text(page, url: str) -> str:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=28_000)
        await page.wait_for_timeout(4_500)   # let JS hydrate

        # Try offer card containers first — cleaner text
        for sel in _CARD_SELECTORS:
            els = await page.query_selector_all(sel)
            if len(els) >= 3:
                texts = [await e.inner_text() for e in els]
                combined = "\n\n".join(t.strip() for t in texts)
                if any(b in combined for b in BANKS):
                    print(f"    selector '{sel}' matched {len(els)} elements")
                    return combined

        # Fall back to full body text
        return await page.inner_text("body")

    except PWTimeout:
        print(f"    timeout on {url}")
        return ""
    except Exception as exc:
        print(f"    error on {url}: {exc}")
        return ""

async def scrape_platform(browser, platform: dict) -> list[dict]:
    page = await browser.new_page()
    await page.set_extra_http_headers({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-IN,en;q=0.9",
    })

    all_text = ""
    for url in platform["urls"]:
        print(f"  → {url}")
        text = await _page_text(page, url)
        all_text += "\n\n" + text
        # If we already have bank-mention-rich content, skip remaining URLs
        if len(text) > 400 and sum(1 for b in BANKS if b in text) >= 2:
            break

    await page.close()

    offers = parse_offers(all_text)
    print(f"  ✓ {len(offers)} offers extracted")
    return offers

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    output: dict = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "platforms": {},
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )

        for platform in PLATFORMS:
            print(f"\n▶ {platform['id']}")
            try:
                output["platforms"][platform["id"]] = await scrape_platform(browser, platform)
            except Exception:
                print(f"  ✗ failed\n{traceback.format_exc()}")
                output["platforms"][platform["id"]] = []

        await browser.close()

    # Write alongside index.html (repo root)
    out = Path(__file__).parent.parent / "offers.json"
    out.write_text(json.dumps(output, indent=2, ensure_ascii=False))

    total = sum(len(v) for v in output["platforms"].values())
    print(f"\n✅  Done — {total} offers written to {out}")


if __name__ == "__main__":
    asyncio.run(main())
