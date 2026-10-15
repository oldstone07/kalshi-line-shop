#!/bin/zsh
# Fallback if GitHub Actions can't reach Kalshi or SharpAPI: run the same fetch on this Mac and push.
# Key is read from ~/.config/kalshi-line-shop/sharpapi_key (mode 600), never from the repo.
set -e
cd "${0:A:h}"
export SHARPAPI_KEY="$(cat ~/.config/kalshi-line-shop/sharpapi_key 2>/dev/null)"
git pull --rebase -q origin main
/usr/bin/python3 fetch_lines.py
git add lines.json
git diff --cached --quiet || { git commit -qm "lines (mac): $(date -u +%Y-%m-%dT%H:%MZ)"; git push -q origin main; }
