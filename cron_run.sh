#!/bin/bash
# Wrapper for cron: sleeps a random offset before running run.py.
# Usage: cron_run.sh [--send-digest | --validate | (no arg = scrape)]
#
# MAX_DRIFT_SECS controls the +/- jitter window:
#   1200 = up to +20 min from the cron schedule time
#   2400 = up to +40 min  (use this when scheduling 20 min early for ±20 min symmetry)
MAX_DRIFT_SECS=2400

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"
LOG="$SCRIPT_DIR/logs/cron.log"

mkdir -p "$SCRIPT_DIR/logs"

# Random sleep using perl (portable across bash/sh/zsh in cron environments)
SLEEP_SECS=$(perl -e "print int(rand($MAX_DRIFT_SECS))")
echo "$(date '+%Y-%m-%d %H:%M:%S')  [cron_run] sleeping ${SLEEP_SECS}s before run ($*)" >> "$LOG"
sleep "$SLEEP_SECS"

cd "$SCRIPT_DIR" || exit 1
"$PYTHON" run.py "$@" >> "$LOG" 2>&1
