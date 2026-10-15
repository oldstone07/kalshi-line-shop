# Kalshi Line Shop

NFL moneylines, three ways: FanDuel/DraftKings, Kalshi tap-buy (ask + taker fee), and Kalshi limit at the bid.
Compares prices only. No picks, no order placement.

- `fetch_lines.py` pulls Kalshi (public API), FD + DK (SharpAPI free tier, `SHARPAPI_KEY` secret), and the
  nflverse schedule, does all the math, and writes `lines.json`. Stdlib only.
- `index.html` reads `lines.json` and renders. The quick calculator works offline.
- `test_math.py` holds the acceptance table from the brief: `python3 test_math.py`
- `.github/workflows/fetch.yml` runs every 30 min Thu–Mon and every 3 h Tue–Wed, plus a manual "Run workflow" button.
- `run_local.sh` is the Mac/launchd fallback if Actions can't reach either API.

If SharpAPI fails or has no line for a game, that game falls back to the nflverse consensus line and is labelled "Consensus".
