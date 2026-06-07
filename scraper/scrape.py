#!/usr/bin/env python3
"""
Flight offer scraper for Indian booking platforms.

Usage:
    python scraper/scrape.py                          # normal run
    python scraper/scrape.py --debug                  # saves raw page text to scraper/debug/
    python scraper/scrape.py --debug --platform Cleartrip
"""

import argparse
import asyncio
import json
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── Config ─────────────────────────────────────────────────────────────────

BANKS = [
    "HDFC", "SBI", "State Bank", "Axis", "ICICI", "Kotak",
    "Amex", "American Express", "IndusInd", "RBL",
    "IDFC First", "IDFC", "AU Bank", "AU Small Finance",
    "Yes Bank", "Federal Bank", "Bank of Baroda", "BOB",
    "Standard Chartered", "StanChart", "Canara",
]

_NOT_CODES = {
    "HDFC","ICICI","AXIS","KOTAK","AMEX","INDUSIND","IDFC","EMI","UPI",
    "RBL","SBI","BANK","CARD","FLAT","FREE","BOOK","SAVE","DEAL","EASY",
    "BEST","HOME","FLIGHT","HOTEL","BUS","TRAIN","OFFER","CODE","PROMO",
    "VISA","MASTER","RUPAY","CREDIT","DEBIT","APPLY","LOGIN","SIGN",
    "VIEW","MORE","KNOW","TERMS","CLICK","HERE","NEXT","BACK",
}

PLATFORMS = [
    {
        "id": "MakeMyTrip",
        "urls": [
            "https://www.makemytrip.com/offers/",
            "https://www.makemytrip.com/offers/flights-offers.html",
        ],
    },
    {
        "id": "GoIbibo",
        "urls": [
            "https://www.goibibo.com/offers/",
        ],
    },
    {
        "id": "Yatra",
        "urls": [
            "https://www.yatra.com/offers/",
            "https://www.yatra.com/flights/domestic-flights.html",
        ],
    },
    {
        "id": "Cleartrip",
        "urls": [
            "https://www.cleartrip.com/offers/",
        ],
    },
    {
        "id": "EaseMyTrip",
        "urls": [
            "https://www.easemytrip.com/offers/flight-offers",
        ],
    },
    {
        "id": "Ixigo",
        "urls": [
            "https://www.ixigo.com/offers/",
        ],
    },
]

# ── Extraction helpers ──────────────────────────────────────────────────────

def _bank_in(text: str) -> str | None:
    for bank in BANKS:
        if re.search(r'\b' + re.escape(bank) + r'\b', text, re.IGNORECASE):
            return bank
    return None

def _discount(text: str) -> str | None:
    """Match rupee amounts and percentages in all common Indian formats."""
    patterns = [
        # ₹ symbol variants
        (r'(?:flat|upto|up\s+to|save|get|off|instant)?\s*[₹]\s*([\d,]+)', '₹{}'),
        # Rs. / Rs / INR variants
        (r'(?:flat|upto|up\s+to|save|get|off|instant)?\s*(?:Rs\.?|INR)\s*([\d,]+)', '₹{}'),
        # percentage
        (r'(\d{1,3})\s*%\s*(?:off|instant\s+discount|cashback|discount|savings)', '{}% off'),
        # "discount of X" without symbol — less reliable, check last
        (r'(?:discount|savings?|cashback)\s+of\s+(?:Rs\.?|₹|INR)?\s*([\d,]+)', '₹{}'),
    ]
    for pat, fmt in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).replace(',', '')
            if val.isdigit() and int(val) < 10:
                continue  # skip tiny numbers like "5% off navigation arrows"
            return fmt.format(m.group(1))
    return None

def _promo_code(text: str) -> str | None:
    m = re.search(
        r'(?:use\s+code|coupon|promo\s+code|apply\s+code)[:\s]+([A-Z][A-Z0-9]{3,11})\b',
        text, re.IGNORECASE,
    )
    if m:
        code = m.group(1).upper()
        if code not in _NOT_CODES:
            return code
    for tok in re.findall(r'\b([A-Z][A-Z0-9]{4,11})\b', text):
        if tok not in _NOT_CODES and not tok.isalpha() and not tok.isdigit():
            return tok
    return None

def _validity(text: str) -> str | None:
    for pat in [
        r'valid\s+(?:till|until|through)[:\s]+([^\n.]{3,30})',
        r'expires?\s*(?:on)?[:\s]+([^\n.]{3,30})',
        r'offer\s+ends?\s*[:\s]+([^\n.]{3,30})',
        r'(\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*\d{2,4})',
        r'(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:30]
    return None

def _min_booking(text: str) -> str | None:
    m = re.search(
        r'(?:min(?:imum)?\s+(?:booking|txn|transaction|purchase|order|spend)[:\s]+(?:Rs\.?|₹|INR)?\s*([\d,]+)'
        r'|on\s+(?:a\s+)?(?:minimum\s+)?(?:booking|transaction)\s+of\s+(?:Rs\.?|₹|INR)?\s*([\d,]+))',
        text, re.IGNORECASE,
    )
    if m:
        return f"₹{(m.group(1) or m.group(2))}"
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
    offers, seen = [], set()
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
        if len(offers) >= 25:
            break
    return offers

# ── Playwright helpers ──────────────────────────────────────────────────────

_CARD_SELECTORS = [
    "[class*='offer-card']", "[class*='offerCard']", "[class*='offer_card']",
    "[class*='bank-offer']", "[class*='bankOffer']", "[class*='deal-card']",
    "[class*='dealCard']",   "[class*='promo-card']", "article",
]

async def scroll_page(page):
    """Scroll to trigger lazy-loaded content."""
    for _ in range(5):
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await page.wait_for_timeout(600)

async def get_page_text(page, url: str) -> tuple[str, str]:
    """Returns (text, method_description)."""
    try:
        print(f"    navigating → {url}")
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        await page.wait_for_timeout(5_000)
        await scroll_page(page)

        # Dismiss cookie/consent overlays
        for sel in [
            "button[id*='accept']", "button[class*='accept']",
            "button[class*='agree']", "#onetrust-accept-btn-handler",
            "button:has-text('Accept')", "button:has-text('Got it')",
        ]:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(800)
                    break
            except Exception:
                pass

        # Try offer card containers first
        for sel in _CARD_SELECTORS:
            els = await page.query_selector_all(sel)
            if len(els) >= 3:
                texts = [await e.inner_text() for e in els]
                combined = "\n\n".join(t.strip() for t in texts if t.strip())
                if any(b.lower() in combined.lower() for b in BANKS):
                    return combined, f"selector:{sel}({len(els)})"

        body = await page.inner_text("body")
        return body, "body-fallback"

    except PWTimeout:
        return "", "timeout"
    except Exception as exc:
        return "", f"error:{exc}"


async def scrape_platform(browser, platform: dict, debug: bool, debug_dir: Path) -> list[dict]:
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
        text, method = await get_page_text(page, url)
        bank_hits = sum(1 for b in BANKS if b.lower() in text.lower())
        print(f"    method={method}  chars={len(text)}  bank_hits={bank_hits}")
        all_text += "\n\n" + text
        if len(text) > 400 and bank_hits >= 2:
            break

    await page.close()

    if debug:
        out = debug_dir / f"{platform['id']}.txt"
        out.write_text(all_text.strip(), encoding="utf-8")
        print(f"    debug → {out}  ({len(all_text)} chars)")

    offers = parse_offers(all_text)
    print(f"    {'✓' if offers else '✗'} {len(offers)} offers extracted")

    if debug and not offers and all_text.strip():
        snippet = all_text.strip()[:600].replace('\n', ' ↵ ')
        print(f"    [snippet] {snippet}\n")

    return offers


# ── Main ────────────────────────────────────────────────────────────────────

async def main(args) -> None:
    targets = PLATFORMS
    if args.platform:
        targets = [p for p in PLATFORMS if p["id"].lower() == args.platform.lower()]
        if not targets:
            names = ', '.join(p['id'] for p in PLATFORMS)
            print(f"Unknown platform '{args.platform}'. Valid: {names}")
            return

    debug_dir = Path(__file__).parent / "debug"
    if args.debug:
        debug_dir.mkdir(exist_ok=True)
        print(f"Debug ON — raw text → {debug_dir}/\n")

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "platforms": {p["id"]: [] for p in PLATFORMS},
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        for platform in targets:
            print(f"\n▶  {platform['id']}")
            try:
                output["platforms"][platform["id"]] = await scrape_platform(
                    browser, platform, args.debug, debug_dir
                )
            except Exception:
                print(f"   ✗ unexpected error\n{traceback.format_exc()}")

        await browser.close()

    out_path = Path(__file__).parent.parent / "offers.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    total = sum(len(v) for v in output["platforms"].values())
    print(f"\n✅  {total} total offers → {out_path}")

    if args.debug and total == 0:
        print(f"\n⚠  Nothing extracted. Open the .txt files in {debug_dir}/ and look for")
        print("   where bank names + amounts appear, then paste a sample here.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--debug",    action="store_true")
    p.add_argument("--platform", type=str, default=None)
    asyncio.run(main(p.parse_args()))
