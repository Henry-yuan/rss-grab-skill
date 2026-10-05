#!/bin/bash
# RSS 订阅增量拉取（launchd 定时任务入口）—— V1.1 T3.1，2026-10-05
#
# 这个脚本 2026-09-19 停用时被删除，导致 plist 指向一个不存在的文件，
# launchd 每天 22:30 静默失败（exit 127）三个多月无人察觉。
# **plist 里的绝对路径与本文件必须同时修**，只修一边仍然是死的。
#
# 它做什么：遍历 rss/订阅/subscriptions.json 里的全部源 →
#   拉增量 → AI 摘要 → 写状态文件「待确认」区
# 它**不**做什么：阶段 3（笔记生成）在 T3.3 才接上，现在这一步**只做摘要、不下载音频**。

set -uo pipefail   # 刻意不用 -e：单源失败不该中断其余源

# ---- 路径（一律绝对，不依赖 launchd 的 cwd 与 PATH）----
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# rss-grab 在主项目里是指向 opensource 仓库的**软链**，用 readlink -f 解析真实路径
SKILL_DIR="$(readlink -f "$SCRIPT_DIR")"
PROJECT_ROOT="$(cd -- "$SKILL_DIR/../../../.." && pwd)"   # .agents/skills/rss-grab/scripts → 项目根
SCRIPTS="$SKILL_DIR"

LOG_DIR="$PROJECT_ROOT/rss/_logs"
LOG_FILE="$LOG_DIR/fetch-updates-$(date +%Y%m%d).log"

# 必须用 3.12（系统 /usr/bin/python3 是 3.9.6，缺依赖）
PY="/usr/local/bin/python3"

mkdir -p "$LOG_DIR" 2>/dev/null || true

# ⚠️ 2026-10-05 修：原实现用 `... | tee -a "$LOG_FILE"`，两个问题：
#   ① LOG_DIR 不存在时 tee 先报错，log() 整条失效（错误信息反而看不见）
#   ② pipefail 会让退出码变成 tee 的（目录不存在时 tee 非 0 → 整个脚本非 0，
#      明明该报「路径错」却看不出是哪一步）
# 改成直接追加写文件，退出码只由 log 自己决定。
log() {
  local line
  line="$(printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*")"
  printf '%s\n' "$line"
  printf '%s\n' "$line" >> "$LOG_FILE" 2>/dev/null || true
}

# ---- 前置检查：路径错了就立刻退出，不要静默失败（V1.0 的教训）----
if [ ! -x "$PY" ]; then
  log "❌ 找不到 $PY（需 Python 3.12）"; exit 1
fi
if [ ! -f "$SCRIPTS/fetch_rss_feed.py" ]; then
  log "❌ 找不到 fetch_rss_feed.py（$SCRIPTS）—— plist 路径可能又变了"; exit 1
fi
if [ ! -f "$PROJECT_ROOT/rss/订阅/subscriptions.json" ]; then
  log "❌ 找不到 subscriptions.json（$PROJECT_ROOT）—— 项目根可能推错了"; exit 1
fi

# ---- 单实例锁：上一轮没跑完就别再叠一轮（launchd 不会自动等）----
# ⚠️ 2026-10-05 修：原实现用 flock —— **macOS 自带 sh 与绝大多数环境都没有
# 这个命令**（实测本机就没有），于是单实例保护长期形同虚设，只打印一行警告
# 就继续跑，两轮叠一起会同时打 LLM 更容易撞限流。
# 改用 `mkdir` 的**原子性**做锁：mkdir 同一路径时，第二次必然失败（ENOENT/EEXIST），
# 这是 POSIX 保证的，不依赖任何外部命令。
LOCK_DIR="/tmp/rss-fetch-updates.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  # 可能是上一轮崩溃留下的陈旧锁（此时目录里会有 pid 文件）
  if [ -f "$LOCK_DIR/pid" ]; then
    oldpid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
      log "⏭️ 上一轮（pid $oldpid）还在跑，本次跳过"
      exit 0
    fi
    log "🧹 清理陈旧锁（pid ${oldpid:-?} 已不在）"
  fi
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "⏭️ 抢锁失败（可能刚被别的进程抢到），本次跳过"
    exit 0
  fi
fi
echo $$ > "$LOCK_DIR/pid" 2>/dev/null || true
# 无论从哪条路退出（含信号）都清锁，否则一次崩溃就永久挡住后续所有定时任务
trap 'rm -rf "$LOCK_DIR" 2>/dev/null || true' EXIT INT TERM

log "=== 开始增量拉取（项目根 $PROJECT_ROOT）==="

cd "$PROJECT_ROOT" || { log "❌ cd 失败：$PROJECT_ROOT"; exit 1; }
"$PY" "$SCRIPTS/fetch_rss_feed.py" --fetch-updates >> "$LOG_FILE" 2>&1
code=$?

if [ "$code" -eq 0 ]; then
  log "=== 完成：只做了摘要，未生成笔记（阶段 3 见 T3.3）==="
else
  log "=== 失败（exit $code），详见本日志上文 ==="
fi
exit $code
