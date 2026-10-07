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

# ---- Stage 2: pull Feishu state back into md (optional, NEVER blocking) ----
# Why here and not after stage 3: stage 3 picks items by scanning the
# "confirmed" section of the state file. Without this step a checkbox set
# in Feishu never reaches md, so stage 3 cannot see it at all.
#
# FAILURE IS NEVER FATAL. Feishu is an optional view; the collection
# pipeline must keep working with no lark-cli, no credentials, or a dead
# API. A broken Feishu degrades to "states stay as they were", never to
# "nothing gets collected".
if [ -f "$SCRIPTS/lark_sync.py" ]; then
  log "=== stage 2: pull Feishu state into md (optional) ==="
  "$PY" "$SCRIPTS/lark_sync.py" sync >> "$LOG_FILE" 2>&1
  sync_code=$?
  if [ "$sync_code" -eq 0 ]; then
    log "=== stage 2 done (Feishu in sync) ==="
  else
    log "WARN: stage 2 returned $sync_code (Feishu unavailable or not configured); continuing anyway"
  fi
else
  log "WARN: lark_sync.py not found under $SCRIPTS; stage 2 skipped (this is fine)"
fi

# ---- Stage 3: generate notes for items the user marked [x] (T3.3) ----
# Runs even when stage 1 failed: its input is the state file, not the feed.
# Only [x] items are processed, so unconfirmed episodes cost nothing.
# Successful items are flipped to [done] ONLY after the note artifact is
# verified on disk; a failed generation stays [x] and is retried next night.
if [ -f "$SCRIPTS/generate_note_for_confirmed.py" ]; then
  log "=== stage 3: notes for confirmed ([x]) items ==="
  # Filter out one line of macOS allocator noise. Stage 3 forks a chain of
  # Python subprocesses (v3 pipeline + mlx); each emits
  #   Python(<pid>) MallocStackLogging: can't turn off malloc stack logging
  #   because it was not enabled.
  # It is harmless (the allocator refused to change a state it was not in),
  # but it arrives once per note subprocess -- 38 lines in the 2026-10-07 run --
  # and it drowns out the real summary lines at the end of the log, which is
  # exactly where you look when a run fails.
  # awk (not grep) because grep exits 1 when it filters everything, and
  # `set -o pipefail` would then turn a successful stage into a reported
  # failure. The stage's own exit code is read from PIPESTATUS[0], so the
  # filter cannot affect it either way.
  "$PY" "$SCRIPTS/generate_note_for_confirmed.py" --all 2>&1 \
    | awk '!/MallocStackLogging: can.t turn off malloc stack logging/' \
    >> "$LOG_FILE"
  note_code=${PIPESTATUS[0]}
  if [ "$note_code" -eq 0 ]; then
    log "=== stage 3 done (nothing to do, or all notes ok) ==="
  else
    log "=== stage 3 returned $note_code (some notes FAILED); stage 1 result is unaffected ==="
  fi
else
  log "WARN: generate_note_for_confirmed.py not found under $SCRIPTS; stage 3 skipped"
fi

# ---- Stage 4: push the system-decided [done] states back to Feishu ----
# Only "converted" is ever written; the other three statuses belong to the
# user and flow md-ward via stage 2. Also non-fatal.
if [ -f "$SCRIPTS/lark_sync.py" ]; then
  log "=== stage 4: push converted ([done]) states to Feishu (optional) ==="
  "$PY" "$SCRIPTS/lark_sync.py" push-status >> "$LOG_FILE" 2>&1
  push_code=$?
  if [ "$push_code" -eq 0 ]; then
    log "=== stage 4 done ==="
  else
    log "WARN: stage 4 returned $push_code (Feishu unavailable); continuing anyway"
  fi
else
  log "WARN: lark_sync.py not found under $SCRIPTS; stage 4 skipped (this is fine)"
fi

exit $code
