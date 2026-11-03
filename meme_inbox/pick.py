#!/usr/bin/env python3
"""按情绪/场合挑一张表情包。用法：
  meme.py                      # 随便一张
  meme.py --mood 得意          # 按情绪
  meme.py --scene 被怼 --mood 嘴硬
  meme.py --list               # 看标签
"""
import argparse, json, os, random, sys
from pathlib import Path

# 索引位置：优先环境变量 MEME_INDEX，其次当前目录下的 memes/index.jsonl
IDX = os.environ.get("MEME_INDEX") or str(Path.cwd() / "memes" / "index.jsonl")

def load():
    rows = []
    try:
        with open(IDX, encoding="utf-8") as fh:
            for l in fh:
                if not l.strip():
                    continue
                try: rows.append(json.loads(l))
                except Exception: pass
    except FileNotFoundError:
        sys.exit(f"找不到索引：{IDX}（用 MEME_INDEX 指定，或用 --index 参数）")
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mood")
    ap.add_argument("--scene")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--n", type=int, default=1)
    a = ap.parse_args()
    rows = load()
    if a.list:
        from collections import Counter
        mc, sc = Counter(), Counter()
        for r in rows:
            for m in r.get("mood", []): mc[m] += 1
            for s in r.get("scene", []): sc[s] += 1
        print("情绪:", "、".join(f"{k}{v}" for k, v in mc.most_common()))
        print("场合:", "、".join(f"{k}{v}" for k, v in sc.most_common()))
        print("总数:", len(rows))
        return
    pick = rows
    if a.mood: pick = [r for r in pick if a.mood in r.get("mood", [])]
    if a.scene: pick = [r for r in pick if a.scene in r.get("scene", [])]
    if not pick:
        print("没有匹配的", file=sys.stderr); sys.exit(1)
    for r in random.sample(pick, min(a.n, len(pick))):
        print(f"{r['file']}\t{'/'.join(r.get('mood',[]))}\t{r['desc'][:50]}")

if __name__ == "__main__":
    main()
