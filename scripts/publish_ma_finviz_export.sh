#!/usr/bin/env bash
# Publish a freshly-exported Finviz CSV for the MA Pullback / Breakout-Retest
# agent so the next daily-ma-scan cloud run can see it -- near-identical to
# scripts/publish_finviz_export.sh, just targeting
# data/ma_pullback/finviz_export.csv (this agent's own export, kept entirely
# separate from the stochastic system's data/finviz_export.csv).
#
# Pulls the latest state FIRST, before copying in the new CSV -- same reason
# as publish_finviz_export.sh: this local machine's data/ directory is
# gitignored and not where the bot's watchlist/positions state actually
# lives day-to-day (the cloud routines maintain that on the bot-state
# branch) -- pushing without pulling first would silently overwrite that
# accumulated state with whatever (likely stale or empty) copy happens to be
# sitting on this machine.
#
# Usage: scripts/publish_ma_finviz_export.sh /path/to/your/finviz_export.csv
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "usage: $0 <path-to-fresh-finviz-export.csv>" >&2
    exit 1
fi

src="$1"
if [ ! -f "$src" ]; then
    echo "publish_ma_finviz_export: $src not found" >&2
    exit 1
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(git -C "$script_dir" rev-parse --show-toplevel)

"$repo_root/scripts/sync_state.sh" pull

dest="$repo_root/data/ma_pullback/finviz_export.csv"
mkdir -p "$repo_root/data/ma_pullback"
cp "$src" "$dest"
echo "publish_ma_finviz_export: copied $src -> $dest"

"$repo_root/scripts/sync_state.sh" push
