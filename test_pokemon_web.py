"""Browser control tests using a local server, without a model or emulator."""

from io import StringIO
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from play_pokemon import Operator, SkyEmu
from pokemon_web import Dashboard


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.operator = Operator("Play")
        self.dashboard = Dashboard(self.operator, "http://emulator.invalid:8080", 0, StringIO())
        self.dashboard.thread = threading.Thread(target=self.dashboard.server.serve_forever, daemon=True)
        self.dashboard.thread.start()
        self.base = f"http://127.0.0.1:{self.dashboard.server.server_port}"
        self.session = requests.Session()
        self.session.trust_env = False

    def tearDown(self):
        self.session.close()
        self.dashboard.close()

    def control(self, data, token=None):
        return self.session.post(self.base + "/control", json=data,
                                 headers={"X-Operator-Token": token or self.dashboard.token}, timeout=3)

    def test_message_resumes_and_invalidates_previously_chosen_action(self):
        version = self.operator.snapshot()[0]
        response = self.control({"message": "Choose the fire starter"})
        self.assertEqual(response.status_code, 200)
        new_version, paused, stopped, messages = self.operator.snapshot()
        self.assertGreater(new_version, version)
        self.assertFalse(paused)
        self.assertFalse(stopped)
        self.assertEqual(messages, ["Play", "Choose the fire starter"])
        emulator = SkyEmu("http://unused")
        emulator.api = Mock()
        self.assertFalse(emulator.tap("A", self.operator, version))
        # Only release; the pending action must never press A.
        self.assertEqual(emulator.api.call_count, 1)
        self.assertFalse(any(emulator.api.call_args.kwargs.values()))

    def test_message_interrupts_a_tap_and_releases_input(self):
        emulator = SkyEmu("http://unused")
        emulator.api = Mock()
        with patch("play_pokemon.time.sleep", side_effect=lambda _: self.control({"message": "Go left"})):
            self.assertFalse(emulator.tap("Right", self.operator, 0))
        self.assertEqual(emulator.api.call_count, 2)
        self.assertFalse(any(emulator.api.call_args.kwargs.values()))

    def test_pause_resume_and_quit(self):
        for command, expected in (("/pause", (True, False)), ("/resume", (False, False)), ("/quit", (False, True))):
            self.assertEqual(self.control({"command": command}).status_code, 200)
            self.assertEqual(self.operator.snapshot()[1:3], expected)
        self.assertEqual(self.control({"message": "Keep going"}).status_code, 400)

    def test_invalid_requests_do_not_change_operator(self):
        for data in ({"message": " "}, {"message": "/quit"}, {"command": "/reset"}, [], {"message": 123}):
            with self.subTest(data=data):
                self.assertEqual(self.control(data).status_code, 400)
                self.assertEqual(self.operator.snapshot(), (0, False, False, ["Play"]))
        self.assertEqual(self.control({"command": "/quit"}, "invalid").status_code, 403)
        self.assertFalse(self.operator.snapshot()[2])

    def test_page_log_and_status(self):
        page = self.session.get(self.base, timeout=3)
        self.assertEqual(page.status_code, 200)
        self.assertIn(self.dashboard.token, page.text)
        self.assertNotIn("__TOKEN__", page.text)
        self.dashboard.write("Live decision\n")
        self.operator.receive("/pause")
        result = self.session.get(self.base + "/events", timeout=3).json()
        self.assertEqual(result["lines"], [[1, "Live decision\n"]])
        self.assertTrue(result["paused"])
        # Browser cannot use an arbitrary Host to obtain the control token.
        self.assertEqual(self.session.get(self.base, headers={"Host": "evil.invalid"}, timeout=3).status_code, 403)

    def test_frame_proxy_and_unavailable_emulator(self):
        # Patch the proxy's constructor only after creating the HTTP test client.
        with patch("pokemon_web.requests.Session") as constructor:
            proxy = constructor.return_value.__enter__.return_value
            proxy.get.return_value.content = b"png-frame"
            frame = self.session.get(self.base + "/screen", timeout=3)
            self.assertEqual(frame.status_code, 200)
            self.assertEqual(frame.content, b"png-frame")
            self.assertEqual(frame.headers["Content-Type"], "image/png")
            proxy.get.assert_called_once_with("http://emulator.invalid:8080/screen", timeout=(2, 3))
            proxy.get.side_effect = requests.ConnectionError("offline")
            self.assertEqual(self.session.get(self.base + "/screen", timeout=3).status_code, 503)

    def test_model_exchange_is_snapshotted_and_updates_after_response(self):
        request = {"messages": [{"content": "original"}]}
        self.dashboard.record_model("request", request)
        request["messages"][0]["content"] = "changed"
        result = self.session.get(self.base + "/model-events", timeout=3).json()
        self.assertEqual(result["exchanges"][0]["request"]["messages"][0]["content"], "original")
        self.assertNotIn("response", result["exchanges"][0])
        unchanged = self.session.get(self.base + f"/model-events?after={result['revision']}", timeout=3).json()
        self.assertNotIn("exchanges", unchanged)
        self.dashboard.record_model("response", {"choices": []})
        updated = self.session.get(self.base + f"/model-events?after={result['revision']}", timeout=3).json()
        self.assertEqual(updated["exchanges"][0]["response"], {"choices": []})
        self.assertGreater(updated["revision"], result["revision"])

    def test_model_exchange_retention_and_host_check(self):
        for index in range(25):
            self.dashboard.record_model("request", {"model": str(index)})
            self.dashboard.record_model("error", "Offline")
        result = self.session.get(self.base + "/model-events", timeout=3).json()
        self.assertEqual(len(result["exchanges"]), 20)
        self.assertEqual(result["exchanges"][0]["id"], 6)
        self.assertEqual(result["exchanges"][-1]["error"], "Offline")
        response = self.session.get(self.base + "/model-events", headers={"Host": "evil.invalid"}, timeout=3)
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
