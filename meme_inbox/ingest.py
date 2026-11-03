#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import errno
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MOODS = {"得意", "委屈", "嘴硬", "开心", "呆", "摸鱼", "凶", "撒娇", "中性"}
SCENES = {"被夸", "被怼", "干饭", "干活", "闲聊"}
MIN_BYTES = 8 * 1024
MIN_SIDE = 64
MAX_ASPECT_RATIO = 4.0
PROBE_TIMEOUT = 15
DESC_TIMEOUT = 120


def warn(message: str) -> None:
    print(f"警告：{message}", file=sys.stderr)


@dataclass
class IndexLine:
    raw: str
    record: dict[str, Any] | None = None


@dataclass
class Stats:
    added: int = 0
    skipped: int = 0
    duplicate: int = 0
    small: int = 0
    dimensions: int = 0

    def skip(self, reason: str = "") -> None:
        self.skipped += 1
        if reason == "duplicate":
            self.duplicate += 1
        elif reason == "small":
            self.small += 1
        elif reason == "dimensions":
            self.dimensions += 1
        elif reason == "not_meme":
            self.not_meme = getattr(self, "not_meme", 0) + 1

    def summary(self) -> str:
        return (
            f"新增 {self.added} 张，跳过 {self.skipped} 张"
            f"（其中重复 {self.duplicate}，太小 {self.small}，"
            f"尺寸不合格 {self.dimensions}，非表情包 {getattr(self,'not_meme',0)}）"
        )


@dataclass
class Known:
    hashes: set[str] = field(default_factory=set)
    files: set[str] = field(default_factory=set)
    legacy_files: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Candidate:
    original: Path
    destination: Path
    sha: str
    source: str


def absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def load_index(path: Path) -> list[IndexLine]:
    lines: list[IndexLine] = []
    try:
        with path.open("r", encoding="utf-8", errors="surrogateescape") as handle:
            for number, raw in enumerate(handle, 1):
                record = None
                try:
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError("记录不是 JSON 对象")
                    if not isinstance(value.get("file"), str) or not value["file"]:
                        raise ValueError("缺少有效 file 字段")
                    record = value
                except (ValueError, TypeError) as exc:
                    warn(f"{path}:{number} 坏行，已跳过：{exc}")
                lines.append(IndexLine(raw, record))
    except FileNotFoundError:
        pass
    return lines


def build_known(lines: list[IndexLine]) -> Known:
    known = Known()
    for line in lines:
        record = line.record
        if record is None:
            continue
        path = str(absolute(record["file"]))
        known.files.add(path)
        sha = record.get("sha")
        if isinstance(sha, str) and sha:
            known.hashes.add(sha.lower())
        else:
            known.legacy_files.add(path)
    return known


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def run_command(args: list[str], timeout: int) -> str:
    result = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        shell=False,
    )
    if result.returncode:
        detail = result.stderr.strip()[:1000]
        raise RuntimeError(f"命令退出码 {result.returncode}：{detail}")
    return result.stdout


def image_dimensions(path: Path) -> tuple[int, int] | None:
    identify = shutil.which("identify")
    if identify:
        try:
            output = run_command(
                [identify, "-ping", "-format", "%w %h\n", f"{path}[0]"],
                PROBE_TIMEOUT,
            )
            width, height = map(int, output.splitlines()[0].split())
            if width > 0 and height > 0:
                return width, height
        except (OSError, ValueError, IndexError, RuntimeError, subprocess.TimeoutExpired):
            pass

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            output = run_command(
                [
                    ffprobe, "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "json", str(path),
                ],
                PROBE_TIMEOUT,
            )
            stream = json.loads(output)["streams"][0]
            width, height = int(stream["width"]), int(stream["height"])
            if width > 0 and height > 0:
                return width, height
        except (
            OSError, ValueError, KeyError, IndexError, TypeError,
            RuntimeError, subprocess.TimeoutExpired,
        ):
            pass
    return None


def valid_tags(value: Any, allowed: set[str], default: str) -> list[str]:
    if not isinstance(value, list):
        return [default]
    result = list(dict.fromkeys(
        item for item in value if isinstance(item, str) and item in allowed
    ))
    return result or [default]


def describe(path: Path, command: str | None) -> dict[str, Any]:
    if not command:
        return {
            "desc": "（待补描述）",
            "mood": ["中性"],
            "scene": ["闲聊"],
            "needs_review": True,
        }
    output = run_command([command, str(path)], DESC_TIMEOUT)
    value = json.loads(output)
    if not isinstance(value, dict):
        raise ValueError("描述命令输出不是 JSON 对象")
    desc = value.get("desc")
    if not isinstance(desc, str) or not desc.strip():
        raise ValueError("描述命令返回的 desc 为空或不是字符串")
    return {
        "desc": " ".join(desc.split())[:100],
        "mood": valid_tags(value.get("mood"), MOODS, "中性"),
        "scene": valid_tags(value.get("scene"), SCENES, "闲聊"),
        "is_meme": bool(value.get("is_meme", True)),
    }


def sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        warn(f"目录 fsync 失败 {path}：{exc}")


def rewrite_index(path: Path, lines: list[IndexLine]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            errors="surrogateescape",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            for line in lines:
                if line.record is None:
                    handle.write(line.raw)
                    if not line.raw.endswith("\n"):
                        handle.write("\n")
                else:
                    handle.write(json.dumps(line.record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
        temporary = None
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def recheck_records(
    path: Path,
    lines: list[IndexLine],
    command: str | None,
    dry_run: bool,
    pool: concurrent.futures.ThreadPoolExecutor,
    judged: set[str] | None = None,
    judge_fails: dict[str, int] | None = None,
) -> None:
    targets = [
        (number, line)
        for number, line in enumerate(lines, 1)
        if line.record is not None and line.record.get("needs_review") is True
    ]
    if not targets:
        return
    if not command:
        warn("--recheck 需要设置 MEME_DESC_CMD；待复核记录保持不变")
        return
    if dry_run:
        for number, line in targets:
            assert line.record is not None
            print(f"[dry-run] 重新描述并原位更新索引第 {number} 行：{line.record['file']}")
        return

    futures: dict[concurrent.futures.Future[dict[str, Any]], tuple[int, IndexLine]] = {}
    for number, line in targets:
        assert line.record is not None
        image = absolute(line.record["file"])
        if not image.is_file():
            warn(f"复核第 {number} 行失败：文件不存在 {image}")
            continue
        futures[pool.submit(describe, image, command)] = (number, line)

    replacements: list[tuple[IndexLine, dict[str, Any]]] = []
    for future in concurrent.futures.as_completed(futures):
        number, line = futures[future]
        try:
            update = future.result()
            assert line.record is not None
            record = dict(line.record)
            record.update(update)
            record.pop("needs_review", None)
            replacements.append((line, record))
        except Exception as exc:
            warn(f"复核第 {number} 行失败：{exc}")

    if replacements:
        old_records = [(line, line.record) for line, _ in replacements]
        for line, record in replacements:
            line.record = record
        try:
            rewrite_index(path, lines)
        except Exception:
            for line, record in old_records:
                line.record = record
            raise
        print(f"已复核更新 {len(replacements)} 条记录", file=sys.stderr)


JUDGED_NAME = "judged.json"
MAX_JUDGE_FAILS = 3   # 同一张图判定失败这么多次就放弃，别无限重判


def load_judged(state_dir: Path) -> set[str]:
    """已经让模型判过的图（不管结论），下次不再送模型。2026-11-03 加。
    之前每轮都把 inbox 里整批图重判一遍，47 张 x 24 轮，钱全烧在这。"""
    path = state_dir / JUDGED_NAME
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("账本顶层不是对象")
    except Exception as exc:  # noqa: BLE001
        # 2026-11-03 修：原来坏文件静默当成"无历史"，正好会重现烧钱事故
        # （账本一坏就整批重判）。这里宁可停本轮，也不静默重新消费。
        raise RuntimeError(
            f"判定账本损坏，本轮中止（避免整批重判）：{path}：{exc}"
        ) from exc
    # 2026-11-03 改：只把"判定成功"或"已失败 3 次以上"的算数。
    # 之前失败不记账，key 一没钱就无限重判同一批图（47 张 × 每小时一轮）。
    out = set()
    for k, v in data.items():
        fails = (v or {}).get("fail", 0) if isinstance(v, dict) else 0
        # 2026-11-03 修：这里原来写反了（fails < MAX），结果失败 1 次的图被当成
        # "判过了"永远跳过，失败 3 次的反而重新拿到重试资格。
        # 正确语义：fail==0 是成功，fail>=MAX 是放弃，两者都算"不再送模型"；
        # 中间那几档要留着重试。
        if fails == 0 or fails >= MAX_JUDGE_FAILS:
            out.add(k.lower())
    return out


def load_fails(state_dir: Path) -> dict[str, int]:
    """读每张图已经失败了几次。2026-11-03 加，配合 MAX_JUDGE_FAILS。"""
    path = state_dir / JUDGED_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("账本顶层不是对象")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"判定账本损坏，本轮中止（避免整批重判）：{path}：{exc}"
        ) from exc
    out: dict[str, int] = {}
    for k, v in data.items():
        if isinstance(v, dict) and v.get("fail"):
            out[k.lower()] = int(v["fail"])
    return out


def save_judged(state_dir: Path, judged: set[str], fails: dict[str, int] | None = None) -> None:
    path = state_dir / JUDGED_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    fails = fails if fails is not None else {}
    # 2026-11-03 修：这里原来不 lower。load_judged/load_fails 都 lower 了，
    # 写的时候不 lower 就会出现 "ABC" 和 "abc" 两条，状态对不上。
    norm_judged = {sha.lower() for sha in judged}
    norm_fails: dict[str, int] = {}
    for raw, count in fails.items():
        key = raw.lower()
        n = int(count)
        if n <= 0:
            continue          # fail=0 不是"待重试"，不写进 fails
        norm_fails[key] = n
    # failed-but-not-yet-abandoned 的也要落盘，否则失败计数每轮清零，
    # MAX_JUDGE_FAILS 永远达不到，key 没钱时照样无限重判。
    keys = norm_judged | set(norm_fails)
    payload = {sha: {"at": now, "fail": norm_fails.get(sha, 0)} for sha in sorted(keys)}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def scan_candidates(
    inbox: Path, library: Path, known: Known, stats: Stats,
    judged: set[str] | None = None,
    judge_fails: dict[str, int] | None = None,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    # 2026-11-03 加：同一批里内容相同的图（不同文件名）原来会各自送一次模型。
    # known.hashes 只在 ingest 阶段更新，扫描阶段拦不住。这里用本批集合挡一次。
    seen_in_batch: set[str] = set()
    source = inbox.name or "inbox"
    # 尺寸规则：短边小于 64 像素，或长短边比例大于 4。
    for image in sorted(inbox.iterdir(), key=lambda item: item.name):
        if image.suffix.lower() not in EXTENSIONS:
            continue
        try:
            if image.is_symlink():
                stats.skip()
                warn(f"跳过符号链接：{image}")
                continue
            if not image.is_file():
                continue
            if image.stat().st_size < MIN_BYTES:
                stats.skip("small")
                continue
            digest = sha256_file(image)
            if digest in known.hashes or str(image) in known.legacy_files:
                stats.skip("duplicate")
                continue
            if digest in seen_in_batch:
                stats.skip("duplicate")
                continue
            if judged and digest.lower() in judged:
                stats.skip("judged")
                continue
            destination = library / f"{source}_{digest[:16]}{image.suffix.lower()}"
            if str(destination) in known.files:
                stats.skip("duplicate")
                continue
            size = image_dimensions(image)
            if size:
                short, long = sorted(size)
                ratio = long / short
                if short < MIN_SIDE or not math.isfinite(ratio) or ratio > MAX_ASPECT_RATIO:
                    stats.skip("dimensions")
                    continue
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f"目标文件存在但未在索引中，拒绝覆盖：{destination}")
            seen_in_batch.add(digest)
            candidates.append(Candidate(image, destination, digest, source))
        except Exception as exc:
            stats.skip()
            warn(f"扫描失败 {image}：{exc}")
    return candidates


def move_image(source: Path, destination: Path) -> None:
    try:
        os.replace(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(str(source), str(destination))


def next_meme_id(index: Path) -> str:
    """读现有索引里最大的编号，+1。2026-11-03 加：图要有个短号，人看着方便。"""
    top = 0
    try:
        for line in index.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                mid = json.loads(line).get("id", "")
            except Exception:
                continue
            if isinstance(mid, str) and mid.startswith("M") and mid[1:].isdigit():
                top = max(top, int(mid[1:]))
    except OSError:
        pass
    return f"M{top + 1:03d}"


def append_record(index: Path, record: dict[str, Any]) -> None:
    index.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(record, ensure_ascii=False) + "\n"
    original_size = index.stat().st_size if index.exists() else 0
    needs_newline = False
    if original_size:
        with index.open("rb") as reader:
            reader.seek(-1, os.SEEK_END)
            needs_newline = reader.read(1) != b"\n"
    try:
        with index.open("a", encoding="utf-8") as handle:
            if needs_newline:
                handle.write("\n")
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        # 本脚本假定索引只有一个写入进程。
        try:
            with index.open("r+b") as handle:
                handle.truncate(original_size)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            warn(f"索引追加回滚失败：{exc}")
        raise


def ingest(
    candidates: list[Candidate],
    index: Path,
    known: Known,
    stats: Stats,
    command: str | None,
    dry_run: bool,
    pool: concurrent.futures.ThreadPoolExecutor,
    judged: set[str] | None = None,
    judge_fails: dict[str, int] | None = None,
    state_dir: Path | None = None,
) -> None:
    # 账本每处理完一张就落盘，所以这里需要知道它在哪。
    if state_dir is None:
        state_dir = index.parent / "state"
    judged = judged if judged is not None else set()
    judge_fails = judge_fails if judge_fails is not None else {}
    futures = {
        candidate: pool.submit(describe, candidate.original, command)
        for candidate in candidates
    } if not dry_run else {}

    for candidate in candidates:
        if candidate.sha in known.hashes or str(candidate.destination) in known.files:
            stats.skip("duplicate")
            if candidate in futures:
                futures[candidate].cancel()
            continue

        moved = False
        try:
            if dry_run:
                print(
                    f"[dry-run] {'调用描述命令' if command else '使用待补描述'}；"
                    f"移动 {candidate.original} -> {candidate.destination}；"
                    f"追加索引 {index}"
                )
            else:
                sha = candidate.sha.lower()
                # 2026-11-03 修：future 抛异常时原来完全不记账也不 continue，
                # 直接往下走入库流程。这里兜住。
                try:
                    description = futures[candidate].result()
                except Exception as exc:  # noqa: BLE001
                    warn(f"判定调用异常 {candidate.original}：{exc}")
                    description = {"desc": "（生成失败）", "is_meme": False,
                                   "mood": [], "scene": [], "_failed": True}
                failed = description.get("_failed") or description.get("desc") == "（生成失败）"
                if failed:
                    # 失败也要记账，否则 key 没钱时会无限重判（47 张 × 每小时）。
                    judge_fails[sha] = judge_fails.get(sha, 0) + 1
                    if judge_fails[sha] >= MAX_JUDGE_FAILS:
                        judged.add(sha)
                        warn(f"判定连续失败 {judge_fails[sha]} 次，放弃：{candidate.original}")
                    stats.skip("judge_failed")
                    # 2026-11-03 修：失败必须立刻落盘，否则中途退出时这一轮的
                    # 失败计数全丢，等于又回到"无限重判"。
                    save_judged(state_dir, judged, judge_fails)
                    # 失败必须 continue。原来往下走会拿
                    # is_meme 的默认值 True 继续入库，把没判过的图塞进库。
                    continue
                # 2026-11-03 修：原来在这里就 judged.add()，但移动/写索引都还没做。
                # 一旦后面失败，这张图就"永久跳过但没入库"，没人管。
                # 改成：真的处理完了（入库或归档）才记账。
                # 不是表情包（截图、网页、文档、立绘原图）就不入库
                if not description.get("is_meme", True):
                    stats.skip("not_meme")
                    # 2026-11-03：判成非表情包的必须挪走。
                    # 之前只 continue，文件留在 _inbox 里，下个小时又整批重判一遍——
                    # 47 张 × 24 次/天，视觉模型一次几千 token，一天白烧几 M。
                    rejected_dir = candidate.original.parent.parent / "_rejected"
                    try:
                        rejected_dir.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(candidate.original),
                                    str(rejected_dir / candidate.original.name))
                        print(f"跳过并归档（不是表情包）：{candidate.original}", file=sys.stderr)
                        judged.add(sha)
                        judge_fails.pop(sha, None)
                        save_judged(state_dir, judged, judge_fails)
                    except Exception as exc:  # noqa: BLE001
                        warn(f"归档非表情包失败：{candidate.original}（{exc}）")
                    continue
                if sha256_file(candidate.original) != candidate.sha:
                    raise RuntimeError("图片在扫描后发生变化，拒绝入库")
                candidate.destination.parent.mkdir(parents=True, exist_ok=True)
                if candidate.destination.exists() or candidate.destination.is_symlink():
                    raise FileExistsError(f"拒绝覆盖已有文件：{candidate.destination}")
                move_image(candidate.original, candidate.destination)
                moved = True
                record = {
                    "id": next_meme_id(index),
                    "file": str(candidate.destination),
                    "meaning": description.get("desc", "").strip(),
                    **description,
                    "at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "sha": candidate.sha,
                    "source": candidate.source,
                }
                append_record(index, record)
                judged.add(sha)
                judge_fails.pop(sha, None)
                save_judged(state_dir, judged, judge_fails)
            known.hashes.add(candidate.sha)
            known.files.add(str(candidate.destination))
            stats.added += 1
        except Exception as exc:
            stats.skip()
            warn(f"入库失败 {candidate.original}：{exc}")
            if moved:
                try:
                    if candidate.original.exists() or candidate.original.is_symlink():
                        raise FileExistsError("原路径已被占用")
                    move_image(candidate.destination, candidate.original)
                except Exception as rollback_exc:
                    warn(f"图片回滚失败，图片保留在 {candidate.destination}：{rollback_exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="微信/QQ 表情包自动入库")
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--lib", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--recheck", action="store_true")
    args = parser.parse_args()

    stats = Stats()
    exit_code = 0
    try:
        inbox = absolute(args.inbox)
        index = absolute(args.index)
        library = absolute(args.lib) if args.lib else index.parent / "library"
        state_dir = index.parent / "state"
        if not inbox.is_dir():
            raise NotADirectoryError(f"inbox 不是目录：{inbox}")
        if inbox.resolve() == library.resolve():
            raise ValueError("--lib 不能与 --inbox 相同")
        command = os.environ.get("MEME_DESC_CMD") or None
        if command:
            command = os.path.expanduser(command)

        lines = load_index(index)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            if args.recheck:
                recheck_records(index, lines, command, args.dry_run, pool)
            known = build_known(lines)
            judged = load_judged(state_dir)
            judge_fails: dict[str, int] = load_fails(state_dir)
            candidates = scan_candidates(inbox, library, known, stats, judged)
            # 每处理完一张就落盘一次（见 ingest 内部）。这里再存一次是兜底，
            # 覆盖"这一轮没有任何候选、但账本被外部改动过"的情况。
            ingest(candidates, index, known, stats, command, args.dry_run, pool,
                   judged, judge_fails, state_dir)
            save_judged(state_dir, judged, judge_fails)
    except Exception as exc:
        warn(str(exc))
        exit_code = 1
    finally:
        print(stats.summary())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())