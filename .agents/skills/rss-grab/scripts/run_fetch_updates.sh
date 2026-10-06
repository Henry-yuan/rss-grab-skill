#!/bin/bash
# RSS incremental fetch (launchd entry) -- V1.1 T3.1, 2026-10-05
#
# This script was deleted on 2026-09-19, leaving the plist pointing at a
# missing file; launchd then failed silently (exit 127) every night for
# 3+ months. NOTE: the plist path and this file must BOTH be fixed.
#
# It walks every source in rss/subscriptions.json:
#   fetch new items -> AI summary -> append to the "pending" section
# of each state file. It does NOT download audio or generate notes (stage 3, T3.3).

set -uo pipefail   # deliberately no -e: one bad source must not stop the rest

# ---- Paths: all absolute; never rely on launchd cwd or PATH ----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# rss-grab is a SYMLINK to the opensource repo; resolve it
SKILL_DIR="$(readlink -f "$SCRIPT_DIR")"
PROJECT_ROOT="$(cd -- "$SKILL_DIR/../../../.." && pwd)"   # (comment translated to ASCII; see git history for the Chinese original)
SCRIPTS="$SKILL_DIR"

LOG_DIR="$PROJECT_ROOT/rss/_logs"
LOG_FILE="$LOG_DIR/fetch-updates-$(date +%Y%m%d).log"

# Must be 3.12 (the system /usr/bin/python3 is 3.9.6 and lacks deps).
PY="/usr/local/bin/python3"

# launchd gives a minimal PATH that does NOT include the pip user-install dir
# where yt-dlp lives. Without this, the audio download step fails with
# FileNotFoundError on every nightly run (it is not on Homebrew's PATH either).
export PATH="$HOME/Library/Python/3.12/bin:$PATH"

mkdir -p "$LOG_DIR" 2>/dev/null || true

# (comment translated to ASCII; see git history for the Chinese original)
# (comment translated to ASCII; see git history for the Chinese original)
# (comment translated to ASCII; see git history for the Chinese original)
# (comment translated to ASCII; see git history for the Chinese original)
# (comment translated to ASCII; see git history for the Chinese original)
log() {
  local line
  line="$(printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*")"
  printf '%s\n' "$line"
  printf '%s\n' "$line" >> "$LOG_FILE" 2>/dev/null || true
}

# (comment translated to ASCII; see git history for the Chinese original)
if [ ! -x "$PY" ]; then
  log "FATAL: $PY not found (need Python 3.12)"; exit 1
fi
if [ ! -f "$SCRIPTS/fetch_rss_feed.py" ]; then
  log "FATAL: fetch_rss_feed.py missing under $SCRIPTS (plist path may have moved)"; exit 1
fi
# NOTE: the state dir name contains non-ASCII (rss/<CN>/). Embedding it as a
# literal in the script breaks under launchd's UTF-8 locale (bash splices the
# following $VAR into what it thinks is a comment). Resolve it by glob instead.
SUB_DIR=$(ls -d "$PROJECT_ROOT"/rss/*/ 2>/dev/null | while read -r d; do
  [ -f "$d/subscriptions.json" ] && echo "$d" && break
done | head -1)
if [ -z "$SUB_DIR" ]; then
  log "FATAL: no rss/*/subscriptions.json found under $PROJECT_ROOT"; exit 1
fi

# ---- Single-instance lock: never stack a second run on top of a live one ----
# NOTE 2026-10-05: the first version used flock, which macOS does NOT ship.
# The guard therefore never worked. Replaced by mkdir (atomic on POSIX),
# plus a pid liveness check and a trap that clears stale locks.
# No external command needed.

LOCK_DIR="/tmp/rss-fetch-updates.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    # a previous run may have crashed and left a stale lock behind
  if [ -f "$LOCK_DIR/pid" ]; then
    oldpid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
      log "SKIP: previous run (pid $oldpid) still alive"
      exit 0
    fi
    log "clearing stale lock (pid ${oldpid:-?} gone)"
  fi
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "SKIP: lost the lock race, another run just won"
    exit 0
  fi
fi
echo $$ > "$LOCK_DIR/pid" 2>/dev/null || true
# Always clear the lock on exit (including signals); otherwise one crash
trap 'rm -rf "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM

log "=== fetch updates start (project root: $PROJECT_ROOT) ==="

cd "$PROJECT_ROOT" || { log "FATAL: cannot cd to $PROJECT_ROOT"; exit 1; }
"$PY" "$SCRIPTS/fetch_rss_feed.py" --fetch-updates >> "$LOG_FILE" 2>&1
code=$?

if [ "$code" -eq 0 ]; then
  log "=== stage 1 ok: fetch + AI summaries done ==="
else
  log "=== stage 1 FAILED (exit $code), see log above ==="
fi

# ---- Stage 3: generate notes for items the user marked [x] (T3.3) ----
# Runs even when stage 1 failed: its input is the state file, not the feed.
# Only [x] items are processed, so unconfirmed episodes cost nothing.
# NOTE: keep every comment in this file ASCII-only. Under launchd's UTF-8
# locale bash splices the following $VAR into what it thinks is a comment
# and aborts with "unbound variable" (cost us 3 months of silent exit 127).
if [ -f "$SCRIPTS/generate_note_for_confirmed.py" ]; then
  log "=== stage 3: notes for confirmed ([x]) items ==="
  "$PY" "$SCRIPTS/generate_note_for_confirmed.py" --all >> "$LOG_FILE" 2>&1
  note_code=$?
  if [ "$note_code" -eq 0 ]; then
    log "=== stage 3 done (nothing to do, or all notes ok) ==="
  else
    log "=== stage 3 returned $note_code (some notes FAILED); stage 1 result is unaffected ==="
  fi
else
  log "WARN: generate_note_for_confirmed.py not found under $SCRIPTS; stage 3 skipped"
fi

exit $code
