name: Scrape flight offers

on:
  workflow_dispatch:   # manual only — re-add schedule once Indian IP is set up

jobs:
  scrape:
    runs-on: ubuntu-latest
    permissions:
      contents: write       # needed to push offers.json back to the repo

    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: true

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          update-environment: true

      - name: Install dependencies
        run: |
          pip install playwright beautifulsoup4 --break-system-packages
          playwright install chromium --with-deps

      - name: Run scraper
        run: python scraper/scrape.py

      - name: Commit updated offers.json
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add offers.json
          git diff --staged --quiet \
            && echo "offers.json unchanged — nothing to commit" \
            || (git commit -m "data: update flight offers [skip ci]" && git push)
