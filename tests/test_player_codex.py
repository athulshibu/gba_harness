import base64
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from codex_backend import codex_settings, request_codex


class CodexBackendTests(unittest.TestCase):
    def payload(self):
        return {"model": "codex/gpt-6.1-sol/high", "messages": [
            {"role": "system", "content": "Play carefully"},
            {"role": "user", "content": [
                {"type": "text", "text": "PREVIOUS screenshot"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b"previous").decode()}},
                {"type": "text", "text": "CURRENT screenshot"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b"current").decode()}},
            ]}], "tools": [{"function": {"parameters": {"type": "object",
                "properties": {"commentary": {"type": "string"}}, "required": [], "additionalProperties": False}}}]}

    def test_parse_model_effort_and_reject_malformed_codex_settings(self):
        self.assertEqual(codex_settings("codex/gpt-6.1-sol/high"), ("gpt-6.1-sol", "high"))
        self.assertIsNone(codex_settings("google/gemini-2.5-flash"))
        for model in ("codex/", "codex/model", "codex//high", "codex/model/high/extra", "codex/model/high\"oops"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                codex_settings(model)

    @patch("codex_backend.shutil.which", return_value="codex.exe")
    def test_cli_arguments_screenshots_schema_usage_and_cleanup(self, _which):
        roots = []

        def run(command, **kwargs):
            root = kwargs["cwd"]
            roots.append(root)
            self.assertEqual(command[command.index("--model") + 1], "gpt-6.1-sol")
            self.assertIn('model_reasoning_effort="high"', command)
            self.assertEqual(command[-1], "-")
            self.assertEqual(kwargs["timeout"], 180)
            self.assertIn("CURRENT screenshot", kwargs["input"])
            self.assertEqual((root / "screenshot-1.png").read_bytes(), b"previous")
            self.assertEqual((root / "screenshot-2.png").read_bytes(), b"current")
            schema = json.loads((root / "action.schema.json").read_text())
            self.assertEqual(schema["required"], ["commentary"])
            (root / "action.json").write_text('{"commentary":"hello"}', encoding="utf-8")
            return Mock(returncode=0, stdout='{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}\n', stderr="")

        with patch("codex_backend.subprocess.run", side_effect=run):
            response = request_codex(self.payload())
        self.assertEqual(response["usage"], {"input_tokens": 10, "output_tokens": 2})
        self.assertEqual(response["model"], "gpt-6.1-sol")
        self.assertTrue(all(not root.exists() for root in roots))

    def test_missing_cli_is_actionable(self):
        with patch("codex_backend.shutil.which", return_value=None), self.assertRaisesRegex(ValueError, "codex login"):
            request_codex(self.payload())

    @patch("codex_backend.shutil.which", return_value="codex.exe")
    def test_process_failure_timeout_and_missing_output(self, _which):
        for outcome, expected in ((Mock(returncode=1, stderr="login required"), "login required"),
                                  (subprocess.TimeoutExpired("codex", 180), "timed out"),
                                  (Mock(returncode=0, stdout=""), "no final action")):
            with self.subTest(expected=expected):
                with patch("codex_backend.subprocess.run", side_effect=outcome if isinstance(outcome, Exception) else None,
                           return_value=outcome), self.assertRaisesRegex(ValueError, expected):
                    request_codex(self.payload())
