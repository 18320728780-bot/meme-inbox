#!/bin/bash
# 定时把新收进来的图过一遍，能当表情包的入库。
#
# 需要改的只有下面这几个变量。判图命令通过 MEME_DESC_CMD 传进来，
# 由你自己决定用什么模型（见 desc-gpt.sh / desc-zhipu.sh）。
set -eu

# 新图从哪儿来。可以是聊天软件收到的图、下载目录、截图目录，随便几个。
SRC_DIRS=(
  "$HOME/Pictures/incoming"
)

WORK="${MEME_WORK:-$HOME/memes}"
INBOX="$WORK/_inbox"
LIB="$WORK/library"
IDX="$WORK/index.jsonl"
LOG="${MEME_LOG:-$HOME/logs/meme-ingest.log}"

mkdir -p "$INBOX" "$LIB" "$(dirname "$LOG")"

# 同一时间只允许一个 ingest 在跑。
# 不加锁的话：两个进程会各自扫到同一批图、各自调用模型、
# 各自给索引追加（编号可能重复）、并且互相覆盖账本。
exec 9>"$WORK/.watch.lock"
if ! flock -n 9; then
  echo "$(date '+%F %T') 上一轮还没跑完，跳过" >> "$LOG"
  exit 0
fi

# 把来源目录里最近一天的图拷进 inbox。
#
# 用内容命名（<sha256 前 16 位>.<后缀>），不用原文件名：
#   - 两个来源目录里都叫 photo.png 的两张图不会互相覆盖
#   - 部分写入不会留下一张"看起来完整"的图被拿去判
#   - 重名但内容不同的图（源文件被编辑过）会自然成为两个不同条目
# 已经判过的图由 ingest.py 按内容和账本跳过，这里不用做去重。
STAGE_TMP="$WORK/.stage.tmp"

stage_one() {
  local src="$1"
  local ext sha
  ext="${src##*.}"
  ext="$(printf '%s' "$ext" | tr 'A-Z' 'a-z')"
  sha="$(sha256sum "$src" 2>/dev/null | cut -c1-16)"
  [ -n "$sha" ] || return 0
  [ -e "$INBOX/$sha.$ext" ] && return 0
  cp "$src" "$STAGE_TMP" 2>/dev/null || return 0
  mv -f "$STAGE_TMP" "$INBOX/$sha.$ext" 2>/dev/null || true
}

for d in "${SRC_DIRS[@]}"; do
  [ -d "$d" ] || continue
  while IFS= read -r -d '' f; do
    stage_one "$f"
  done < <(find "$d" -maxdepth 1 -type f \
             \( -iname "*.png" -o -iname "*.jpg" -o -iname "*.jpeg" \
                -o -iname "*.gif" -o -iname "*.webp" \) \
             -mtime -1 -print0 2>/dev/null)
done

export MEME_DESC_CMD="${MEME_DESC_CMD:-$HOME/bin/meme_desc.sh}"

# 保留退出码：原来用 || true 会把 ingest 的故障伪装成调度成功，出问题看不出来。
rc=0
OUT=$(cd "$WORK" && python3 -m meme_inbox.ingest \
        --inbox "$INBOX" --index "$IDX" --lib "$LIB" 2>&1) || rc=$?
echo "$(date '+%F %T') rc=$rc $OUT" >> "$LOG"

# 判成表情包的会被 ingest.py 移进 library，判成非表情包的移进 _rejected，
# 剩下的就是判定失败、还在等重试的 —— 留着，下一轮接着试。
# 只清掉堆积超过 3 天的，免得一直失败的图把 inbox 撑爆。
find "$INBOX" -type f -mtime +3 -delete 2>/dev/null || true
