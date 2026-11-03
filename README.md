# meme-inbox

Turn incoming images into a tagged, deduplicated meme library.

Point it at a folder where your chat bot drops incoming images. It decides which
ones are actually memes, asks a vision model for a one-line description plus
mood/scene tags, deduplicates by content hash, files them into a library folder,
and appends to a JSONL catalog you can query.

Screenshots, web pages, chat transcripts and character art are rejected — they
are not memes.

## Pipeline

```
inbox → filter → describe (vision model) → validate JSON → dedupe (sha256)
      → move to library → append to index.jsonl
```

## Catalog format

One JSON object per line:

```json
{"id": "M042",
 "file": "/abs/path/library/wx_8afe850f9f3be850.png",
 "meaning": "蓝发女仆装Q版角色张嘴笑，配字「token吃完了！」，调侃额度用光。",
 "desc": "蓝发女仆装Q版角色张嘴笑，配字「token吃完了！」，调侃额度用光。",
 "mood": ["开心"], "scene": ["闲聊"], "is_meme": true,
 "at": "2026-11-02T23:24:31+0800",
 "sha": "8afe850f...", "source": "inbox"}
```

`id` is a short human-facing label (`M001`, `M002`, …) assigned on insert.
`meaning` is the same sentence as `desc`, kept as the field you read when you
are scrolling the catalog by hand; `desc` stays for compatibility.

- `mood` ∈ {得意, 委屈, 嘴硬, 开心, 呆, 摸鱼, 凶, 撒娇, 中性}
- `scene` ∈ {被夸, 被怼, 干饭, 干活, 闲聊}

Both are configurable. Out-of-range tags are dropped rather than failing the
whole record.

## Usage

```bash
export MEME_DESC_CMD=/path/to/your-describer
python3 -m meme_inbox.ingest --inbox ./inbox --index ./index.jsonl

# pick one
python3 -m meme_inbox.pick --mood 得意 --scene 被怼
python3 -m meme_inbox.pick --list      # tag distribution
```

## The describer contract

An executable that takes one argument (image path) and prints **one line of
JSON** to stdout:

```json
{"is_meme": true, "text": "图上的字", "desc": "40–80 字中文描述",
 "mood": ["开心"], "scene": ["闲聊"]}
```

Diagnostics go to stderr. `examples/desc-gpt.sh` and `examples/desc-glm.sh` are
working adapters; the core never hardcodes a vendor.

With no `MEME_DESC_CMD` set, the ingester still runs — it writes a
`needs_review: true` placeholder instead of calling a model.

## Why GPT over GLM (an honest note)

On a 40-image batch of mostly screenshots, `gpt-6-astra` accepted 1 and
correctly rejected 39. `glm-4v-plus` accepted 7, of which 2 were chat
screenshots misread as memes. `glm-4v-flash` could not read text on the image
at all.

That is **one anecdote on one batch**, not an accuracy benchmark. If you are
choosing a model, run your own set — the contract is deliberately
vendor-neutral so you can.

## Operational notes

- `sha256` only removes **byte-identical** files. Re-encoded or cropped copies
  will slip through.
- Files are processed with a 3-way thread pool; the describer must tolerate
  concurrent calls.
- Failed descriptions are retried, but with a cap. See **Judge ledger** below —
  this is the part that cost real money once.
- The index is append-only JSONL. `--recheck` rewrites it atomically to
  re-describe anything flagged `needs_review`.

## Judge ledger

Every image the describer has seen is remembered in `state/judged.json`, keyed
by sha256. It records two things per image: when it was judged, and how many
times the call has failed.

```json
{"8afe850f...": {"at": "2026-11-02T23:24:31+0800", "fail": 0}}
```

Reading it back:

| `fail` | meaning | next round |
|---:|---|---|
| `0` | judged successfully | skipped, never sent again |
| `1 .. MAX-1` | call failed | **retried** |
| `>= MAX` (`MAX_JUDGE_FAILS`, default 3) | gave up | skipped |

Three rules here, each one paid for:

1. **A failed call is still recorded.** The first version only recorded
   successes, on the theory that a transient error should get another shot. But
   a vision key with no credit fails *every* time — so nothing was ever
   recorded, and every hourly run re-judged the same 47 images from scratch.
   A tolerance rule met a persistent failure and became an infinite loop.
2. **The failure count has to be persisted, not just held in memory.** If it
   resets each round, the cap is unreachable and rule 1 does nothing.
3. **A corrupt ledger aborts the round instead of being treated as empty.** The
   silent-empty path re-sends everything, which is the exact failure mode the
   ledger exists to prevent. Better to stop and let you look.

If the ledger file cannot be read, `ingest` exits with a non-zero status and
touches nothing.

## Privacy

**Images are uploaded to whichever vision service you configure.** Chat
screenshots regularly contain names, phone numbers and private conversations.
Read `docs/privacy.md`. Keep real images out of Git.

## Status

See `TODO.md`. Not yet stable.

## License

MIT for code. Images are **not** covered — ship your own or use clearly
licensed samples.
