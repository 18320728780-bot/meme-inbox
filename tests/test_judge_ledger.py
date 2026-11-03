"""判定账本（judged.json）的回归测试。

这些用例盯着一个真实事故：同一批 47 张图被判了十遍，一天烧掉 24.6M token。
根因是"判定失败不记账，留着下轮重试" —— key 余额不足时每次都失败，
于是永远不记账，下一轮从零再判一遍。**为容错写的规则，在持续失败时变成了无限循环。**

所以这里测的都是几个"钱会漏出去"的口子：
1. 判过的图不能被重新送模型
2. 失败要有次数上限，且上限必须落盘
3. 账本坏了宁可停本轮，也不能静默当成"无历史"
"""
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meme_inbox import ingest as mi

SHA = "a" * 64


def write_ledger(state_dir: Path, payload) -> None:
    (state_dir / "judged.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


class JudgeStateSemantics(unittest.TestCase):
    """fail 的档位：0 是成功，1..MAX-1 留着重试，>=MAX 放弃。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def skipped(self, fail: int) -> bool:
        write_ledger(self.state, {SHA: {"at": "x", "fail": fail}})
        return SHA in mi.load_judged(self.state)

    def test_success_is_skipped(self):
        self.assertTrue(self.skipped(0))

    def test_retryable_failures_are_not_skipped(self):
        # 这里正是那次写反的地方：失败 1 次被当成"判过了"，永远不再重试。
        for n in range(1, mi.MAX_JUDGE_FAILS):
            with self.subTest(fail=n):
                self.assertFalse(self.skipped(n))

    def test_abandoned_is_skipped(self):
        # 写反的时候，达到上限的反而重新拿到了重试资格，等于无限循环。
        for n in (mi.MAX_JUDGE_FAILS, mi.MAX_JUDGE_FAILS + 5):
            with self.subTest(fail=n):
                self.assertTrue(self.skipped(n))


class JudgeLedgerPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_failure_count_survives_round_trip(self):
        # 计数必须落盘。每轮清零的话 MAX_JUDGE_FAILS 永远达不到。
        mi.save_judged(self.state, set(), {SHA: 2})
        self.assertEqual(mi.load_fails(self.state)[SHA], 2)
        self.assertNotIn(SHA, mi.load_judged(self.state))

    def test_success_does_not_persist_a_failure_count(self):
        mi.save_judged(self.state, {SHA}, {SHA: 0})
        raw = json.loads((self.state / "judged.json").read_text(encoding="utf-8"))
        self.assertEqual(raw[SHA]["fail"], 0)
        self.assertEqual(mi.load_fails(self.state), {})

    def test_keys_are_normalized_to_lowercase(self):
        mi.save_judged(self.state, {SHA.upper()}, {SHA.upper(): 1})
        raw = json.loads((self.state / "judged.json").read_text(encoding="utf-8"))
        self.assertEqual(list(raw), [SHA])
        self.assertEqual(raw[SHA]["fail"], 1)

    def test_missing_ledger_is_a_cold_start(self):
        self.assertEqual(mi.load_judged(self.state), set())
        self.assertEqual(mi.load_fails(self.state), {})


class CorruptLedgerStopsTheRound(unittest.TestCase):
    """账本一坏就"当没有历史"重新判一遍 —— 正好是烧钱事故的复现路径。"""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_broken_json_raises(self):
        write_ledger(self.state, "{这不是 json")
        for load in (mi.load_judged, mi.load_fails):
            with self.subTest(loader=load.__name__):
                with self.assertRaises(RuntimeError):
                    load(self.state)

    def test_non_object_top_level_raises(self):
        for payload in ("[]", '"x"', "null", "3"):
            with self.subTest(payload=payload):
                write_ledger(self.state, payload)
                with self.assertRaises(RuntimeError):
                    mi.load_judged(self.state)


class MemeIds(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.index = Path(self.tmp.name) / "index.jsonl"
        self.addCleanup(self.tmp.cleanup)

    def test_first_id(self):
        self.assertEqual(mi.next_meme_id(self.index), "M001")

    def test_increments_past_existing(self):
        rows = [{"id": "M001"}, {"id": "M007"}, {"id": "M003"}]
        self.index.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        self.assertEqual(mi.next_meme_id(self.index), "M008")

    def test_ignores_rows_without_ids(self):
        rows = [{"file": "x"}, {"id": "M004"}]
        self.index.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        self.assertEqual(mi.next_meme_id(self.index), "M005")


if __name__ == "__main__":
    unittest.main()
