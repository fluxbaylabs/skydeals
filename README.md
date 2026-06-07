# ✈ FlightDeals Scout

Credit card & bank discount offers on Indian flight booking platforms — aggregated daily, no API keys, no LLM, hosted free on GitHub Pages.

## How it works

```
GitHub Actions (daily cron)
  └─ scraper/scrape.py  →  offers.json (committed to repo)
                                ↑
                          index.html reads this via fetch()
                          (served by GitHub Pages)
```

## Setup (≈ 10 minutes)

### 1. Fork / create the repo

Create a new public GitHub repo and push all these files to the `main` branch.

### 2. Enable GitHub Pages

Go to **Settings → Pages** and set:
- Source: **Deploy from a branch**
- Branch: `main`, folder `/` (root)

Your site will be live at `https://<your-username>.github.io/<repo-name>/`.

### 3. Run the scraper for the first time

Go to **Actions → Scrape flight offers → Run workflow**.

This populates `offers.json`. After that it runs automatically every day at 08:30 IST.

### 4. Share the link

The GitHub Pages URL is the only thing your friends need — no accounts, no API keys.

---

## Repo structure

```
├── index.html          # Frontend (reads offers.json, no build step)
├── offers.json         # Auto-updated by GitHub Actions
├── scraper/
│   └── scrape.py       # Playwright scraper
└── .github/
    └── workflows/
        └── scrape.yml  # Daily cron + manual trigger
```

## Tuning the scraper

The scraper uses **text extraction + regex** rather than brittle CSS selectors, so it survives most redesigns. If a platform changes significantly and offers stop showing up:

1. Open `scraper/scrape.py`
2. Find the `PLATFORMS` list and update the `urls` for that platform
3. Check `_CARD_SELECTORS` — add any new container class patterns you spot in the browser DevTools
4. Commit and push; trigger the workflow manually to test

## Running locally

```bash
pip install playwright beautifulsoup4
playwright install chromium
python scraper/scrape.py

# then serve the frontend:
python -m http.server 8000
# open http://localhost:8000
```

## Limitations

- These platforms are JS-rendered SPAs — scraping is best-effort and may miss some offers when sites change their structure.
- No real-time data: offers refresh once per day.
- The scraper may occasionally extract noise (non-offer text that happens to mention a bank name + amount) — these can be filtered out by improving the regex patterns in `scrape.py`.
