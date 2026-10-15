#!/bin/zsh
# Fallback for GitHub Actions: runs every 30 min from launchd, but only fetches when Actions is late
# (lines.json older than 25 min after pulling), so the two never double up while Actions is healthy.
# Key is read from ~/.config/kalshi-line-shop/sharpapi_key (mode 600), never from the repo.
set -e
cd "${0:A:h}"
export SHARPAPI_KEY="$(cat ~/.config/kalshi-line-shop/sharpapi_key 2>/dev/null)"
git pull --rebase -q origin main
AGE=$(/usr/bin/python3 -c "import json,datetime as d;u=d.datetime.fromisoformat(json.load(open('lines.json'))['updated']);print(int((d.datetime.now(d.timezone.utc)-u).total_seconds()//60))")
if (( AGE < 25 )); then echo "$(date '+%F %T') Actions is current (${AGE} min), skipping"; exit 0; fi
/usr/bin/python3 fetch_lines.py
git add lines.json snapshots/
git diff --cached --quiet || { git commit -qm "lines (mac): $(date -u +%Y-%m-%dT%H:%MZ)"; git push -q origin main; }
