name: Daily TW Stock Screener

on:
  workflow_dispatch: {}   # 測試版:只允許手動在 GitHub 網頁上按「Run workflow」執行

permissions:
  contents: write

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run screener
        run: python src/run.py

      - name: Commit results
        run: |
          git config user.name "tw-screener-bot"
          git config user.email "bot@users.noreply.github.com"
          git add docs/results.json
          git diff --staged --quiet || git commit -m "chore: update screener results $(date +'%Y-%m-%d %H:%M')"
          git pull --rebase origin main
          git push
