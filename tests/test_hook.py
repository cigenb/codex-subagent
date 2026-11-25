#!/usr/bin/env python3
"""hook.py 的离线测试：附加文本不超过 Claude Code 的 additionalContext 上限，放不下的留给下一次 hook。

用法：python tests/test_hook.py（不调用 Codex）
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.TemporaryDirectory(prefix="codex-sub-hooktest-")
os.environ["CODEX_SUB_STATE_DIR"] = _TMP.name
import hook  # noqa: E402

LIMIT = 10000  # Claude Code 对 additionalContext 的上限


def task(i, size, status="completed"):
    return {"id": f"t{i:07d}", "status": status, "detach": False, "reported": False, "waited": False,
            "text": f"[t{i:07d}] {status}\n" + "x" * size}


class PackTest(unittest.TestCase):
    def test_small_results_all_fit(self):
        text, sent = hook.pack("H", [task(i, 500) for i in range(3)], "codex_status")
        self.assertEqual(len(sent), 3)
        self.assertNotIn("more finished", text)

    def test_large_results_split_across_hooks(self):
        text, sent = hook.pack("H", [task(i, 6000) for i in range(3)], "codex_status")
        self.assertEqual(len(sent), 1)
        self.assertLessEqual(len(text), LIMIT)
        self.assertIn("2 more finished", text)

    def test_single_oversized_result_truncated(self):
        text, sent = hook.pack("H", [task(0, 20000)], "codex_status")
        self.assertEqual(len(sent), 1)
        self.assertLessEqual(len(text), LIMIT)
        self.assertIn('codex_status(ids=["t0000000"], full=true)', text)


class DeliverTest(unittest.TestCase):
    def test_remaining_results_delivered_by_later_hooks(self):
        out = []
        hook.emit = lambda event, text: out.append(text)
        ack = Path(_TMP.name) / "session-1.ack.json"
        data = {"tasks": {t["id"]: t for t in (task(i, 6000) for i in range(3))}}
        for _ in range(3):
            hook.deliver("PostToolUse", ack.with_name("session-1.json"), data, ack)
        self.assertEqual(len(out), 3)
        self.assertTrue(all(len(t) <= LIMIT for t in out))
        self.assertEqual(json.loads(ack.read_text(encoding="utf-8"))["delivered"], ["t0000000", "t0000001", "t0000002"])
        hook.deliver("PostToolUse", ack.with_name("session-1.json"), data, ack)
        self.assertEqual(len(out), 3, "已送达的结果不再重复送达")

    def test_corrupt_ack_is_rebuilt(self):
        out = []
        hook.emit = lambda event, text: out.append(text)
        ack = Path(_TMP.name) / "session-2.ack.json"
        ack.write_text("{broken", encoding="utf-8")
        hook.deliver("PostToolUse", ack.with_name("session-2.json"), {"tasks": {"t0000000": task(0, 100)}}, ack)
        self.assertEqual(len(out), 1)
        self.assertEqual(json.loads(ack.read_text(encoding="utf-8"))["delivered"], ["t0000000"])

    def test_stop_reminds_running_task_once(self):
        out = []
        hook.emit = lambda event, text: out.append(text)
        ack = Path(_TMP.name) / "session-3.ack.json"
        data = {"tasks": {"t0000000": task(0, 0, status="running")}}
        hook.deliver("Stop", ack.with_name("session-3.json"), data, ack)
        hook.deliver("Stop", ack.with_name("session-3.json"), data, ack)
        self.assertEqual(len(out), 1)
        self.assertIn("t0000000", out[0])


if __name__ == "__main__":
    unittest.main()
