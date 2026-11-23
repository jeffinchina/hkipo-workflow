import json
import tempfile
import unittest
import sys
from unittest.mock import patch
from pathlib import Path
from hkipo.writer import write_article, run_codex


class WriterTests(unittest.TestCase):
    def test_prepare_failure_invalidates_old_ready_and_current_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "status.json").write_text(json.dumps({"status": "ready_for_delivery", "bundle_path": "old"}))
            (path / "release.json").write_text('{"old":true}')
            with self.assertRaises(FileNotFoundError):
                write_article({"kind": "offer", "event_id": "new"}, [str(path / "missing.pdf")], path, path / "missing-skill")
            status = json.loads((path / "status.json").read_text())
            self.assertEqual(status["status"], "deferred")
            self.assertNotIn("bundle_path", status)
            self.assertFalse((path / "release.json").exists())
            self.assertEqual(len(list(path.glob("release.previous-*.json"))), 1)

    def fake_cli(self, path, completed=True):
        script = path / "fake-codex"
        script.write_text("#!" + sys.executable + "\n" +
            "import sys,json,pathlib\n"
            "args=sys.argv[1:];sys.stdin.read()\n"
            "dest=pathlib.Path(args[args.index('--output-last-message')+1])\n"
            "dest.write_text(json.dumps({'ok':'yes'}))\n"
            "pathlib.Path('argv.json').write_text(json.dumps(args))\n"
            "print(json.dumps({'type':'thread.started'}))\n" +
            ("print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'{ok:yes}'}}))\n"
             "print(json.dumps({'type':'turn.completed','usage':{}}))\n" if completed else ""))
        script.chmod(0o700)
        return str(script)

    def test_actual_image_argument_and_complete_jsonl_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);image = root / "page.png";image.write_bytes(b"fake-image-fixture")
            binary = self.fake_cli(root)
            with patch("hkipo.writer.quota_gate", return_value={"allowed": True}):
                result = run_codex("test", {"type":"object"}, root / "run", "draft",
                                   images=[image], executable=binary, timeout=10)
            self.assertEqual(result, {"ok":"yes"})
            argv = json.loads((root / "run/argv.json").read_text())
            self.assertEqual(argv[argv.index("--image")+1], str(image.resolve()))
            self.assertIn("--ephemeral", argv)
            self.assertNotIn("resume", argv)
            self.assertTrue(json.loads((root / "run/draft.completion.json").read_text())["success"])

    def test_exit_zero_and_result_without_turn_completed_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);binary = self.fake_cli(root, completed=False)
            with patch("hkipo.writer.quota_gate", return_value={"allowed": True}):
                with self.assertRaisesRegex(RuntimeError, "did not complete"):
                    run_codex("test", {"type":"object"}, root / "run", "draft", executable=binary, timeout=10)


class WriterQuotaDeferralTests(unittest.TestCase):
    def test_only_typed_fresh_quota_returns_structured_deferred(self):
        from hkipo.quota import QuotaDeferred, evaluate_limits
        import time
        now = time.time()
        observation = evaluate_limits({'rateLimits': {'limitId': 'codex', 'primary': {
            'windowDurationMins': 300, 'usedPercent': 80, 'resetsAt': now+300}}}, now=now)
        with tempfile.TemporaryDirectory() as tmp, patch('hkipo.writer._write_article', side_effect=QuotaDeferred(observation)):
            status = write_article({'event_id': 'fake'}, [], tmp, '/fake')
            self.assertEqual(status['status'], 'deferred')
            self.assertEqual(status['quota_defer']['retry_at'], now+420)
            self.assertEqual(json.loads((Path(tmp)/'status.json').read_text())['reason'], 'quota_reserve')


if __name__ == "__main__":
    unittest.main()
