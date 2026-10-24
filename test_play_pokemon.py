import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from play_pokemon import BUTTONS, TAP_SECONDS, RELEASE_SECONDS, ModelDecisionError, Operator, SkyEmu, choose_action, format_decision, load_env, main, parse_model_action, validate_action, verified_request


def action(button="A"):
    return {"buttons": button if isinstance(button, list) else [button], "reason": "Advance dialogue", "memory": "In dialogue",
            "result": {"status": "uncertain", "details": "The previous outcome is not yet clear."},
            "observation": "Dialogue page with a next indicator", "objective": "Finish the introduction",
            "plan": ["Advance this page", "Read the next page"], "expected_result": "A new dialogue page"}


def completion(*actions, finish_reason="tool_calls"):
    return {"choices": [{"finish_reason": finish_reason, "message": {"tool_calls": [
        {"type": "function", "function": {"name": "press_buttons", "arguments": json.dumps(item)}}
        for item in actions
    ]}}]}


class HarnessTests(unittest.TestCase):
    def test_certificate_handshake_retries_keep_verification_enabled(self):
        send = Mock(side_effect=[requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED: EE certificate key too weak"), Mock(status_code=200)])
        with patch("play_pokemon.time.sleep"):
            response = verified_request(send, "https://openrouter.ai/api/v1/models", timeout=5)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(send.call_count, 2)
        for call in send.call_args_list:
            self.assertNotIn("verify", call.kwargs)  # Requests' verified default.

    def test_persistent_certificate_failure_stops_after_three_attempts(self):
        send = Mock(side_effect=requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED: EE certificate key too weak"))
        with patch("play_pokemon.time.sleep"), self.assertRaisesRegex(requests.exceptions.SSLError, "after 3 attempts.*key too weak"):
            verified_request(send, "https://openrouter.ai/api/v1/models")
        self.assertEqual(send.call_count, 3)

    def test_no_automatic_retry_for_failures_after_possible_submission(self):
        for failure in (requests.exceptions.SSLError("bad record mac"), requests.ReadTimeout("timed out")):
            with self.subTest(failure=failure), self.assertRaises(requests.RequestException):
                send = Mock(side_effect=failure)
                try:
                    verified_request(send, "https://openrouter.ai/api/v1/chat/completions")
                finally:
                    self.assertEqual(send.call_count, 1)

    def test_connection_check_does_not_need_credentials_or_emulator(self):
        with patch.dict(os.environ, {}, clear=True), patch("play_pokemon.load_env"), patch("sys.argv", ["play_pokemon.py", "--check-connection"]), patch("play_pokemon.requests.Session") as session, patch("play_pokemon.SkyEmu") as emulator:
            session.return_value.__enter__.return_value.get.return_value.status_code = 200
            self.assertEqual(main(), 0)
            emulator.assert_not_called()
            client = session.return_value.__enter__.return_value
            client.get.assert_called_once_with("https://openrouter.ai/api/v1/models", timeout=(10, 20))
            client.post.assert_not_called()

    def run_mock_game(self, decisions, extra_args=(), environment=None, load_settings=False):
        operator = Operator("Play")
        emulator = Mock()
        emulator.act.side_effect = lambda decision, *_: {"completed_taps": list(decision["buttons"]), "incomplete_tap": None, "skipped_taps": [], "sequence_completed": True}
        frames = []

        def emulator_api(endpoint, **kwargs):
            if endpoint == "screen":
                frames.append(f"frame{len(frames)}".encode())
                return Mock(content=frames[-1])
            return Mock(text="pong")

        emulator.api.side_effect = emulator_api
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, environment if environment is not None else {"OPENROUTER_API_KEY": "test-key", "OPENROUTER_MODEL": "test-model"}, clear=True))
            stack.enter_context(patch("sys.argv", ["play_pokemon.py", "--steps", str(len(decisions)), *extra_args]))
            if not load_settings:
                stack.enter_context(patch("play_pokemon.load_env"))
            stack.enter_context(patch("play_pokemon.SkyEmu", return_value=emulator))
            stack.enter_context(patch("play_pokemon.Operator", return_value=operator))
            stack.enter_context(patch("play_pokemon.threading.Thread"))
            stack.enter_context(patch("play_pokemon.time.sleep"))
            stack.enter_context(patch("builtins.print"))
            choose = stack.enter_context(patch("play_pokemon.choose_action", side_effect=decisions))
            result = main()
        return result, emulator, operator, choose, frames

    def test_default_env_file_is_beside_script(self):
        import play_pokemon

        with patch("play_pokemon.load_env") as loader:
            with patch("sys.argv", ["play_pokemon.py", "--check-connection"]), patch("play_pokemon.check_connection"):
                self.assertEqual(main(), 0)
        loader.assert_called_once_with(Path(play_pokemon.__file__).resolve().with_name(".env"))

    def test_custom_env_file_supplies_defaults_and_respects_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom settings.env"
            path.write_text('OPENROUTER_API_KEY=file-key\nOPENROUTER_MODEL=file/model\nSKYEMU_URL=http://localhost:9090\nSKYEMU_SPEED=2\n', encoding="utf-8")
            for overrides in (False, True):
                with self.subTest(overrides=overrides):
                    args = ["--env-file", str(path)]
                    environment = {}
                    if overrides:
                        environment = {"OPENROUTER_API_KEY": "shell-key", "OPENROUTER_MODEL": "shell/model"}
                        args += ["--model", "cli/model", "--base", "http://localhost:8081", "--speed", "1"]
                    result, emulator, _, choose, _ = self.run_mock_game([action()], args, environment, load_settings=True)
                    self.assertEqual(result, 0)
                    self.assertEqual(choose.call_args.args[1:3], ("shell-key", "cli/model") if overrides else ("file-key", "file/model"))
                    emulator.set_speed.assert_called_once_with(1 if overrides else 2)

    def test_explicit_missing_env_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.env"
            with patch("sys.argv", ["play_pokemon.py", "--env-file", str(path)]), patch("sys.stderr"), patch("play_pokemon.SkyEmu") as emulator:
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)
                emulator.assert_not_called()

    def test_executable_default_and_override(self):
        for configured in (None, "", "custom/SkyEmu.exe"):
            with self.subTest(configured=configured):
                environment = {"OPENROUTER_API_KEY": "test-key", "OPENROUTER_MODEL": "test-model"}
                if configured is not None:
                    environment["SKYEMU_EXE"] = configured
                result, emulator, _, _, _ = self.run_mock_game([action()], environment=environment)
                self.assertEqual(result, 0)
                self.assertEqual(emulator.prepare.call_args.args[0], configured or "SkyEmu-v5-Windows.exe")

    def test_invalid_decision_retries_with_feedback_and_a_fresh_screen(self):
        error = ModelDecisionError("Model omitted nonempty expected_result")
        result, emulator, operator, choose, frames = self.run_mock_game([error, action()])
        self.assertEqual(result, 0)
        emulator.act.assert_called_once()
        self.assertEqual(frames, [b"frame0", b"frame1"])
        retry = choose.call_args_list[1].args
        self.assertEqual(retry[3], b"frame1")
        self.assertIn("expected_result", retry[-1])
        self.assertEqual(retry[5], "No previous observations.")
        self.assertIsNone(retry[7])
        self.assertFalse(operator.snapshot()[1])

    def test_three_invalid_decisions_pause_without_executing_buttons(self):
        result, emulator, operator, choose, frames = self.run_mock_game([
            ModelDecisionError("Missing plan") for _ in range(3)
        ])
        self.assertEqual(result, 0)
        self.assertEqual(choose.call_count, 3)
        emulator.act.assert_not_called()
        self.assertTrue(operator.snapshot()[1])
        self.assertEqual(len(frames), 3)

    def test_empty_expected_result_is_a_recoverable_decision_error(self):
        with self.assertRaisesRegex(ModelDecisionError, "expected_result"):
            parse_model_action(completion(action() | {"expected_result": ""}))

    def test_invalid_json_arguments_are_recoverable(self):
        response = completion(action())
        response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{broken"
        with self.assertRaises(ModelDecisionError):
            parse_model_action(response)

    def test_correction_feedback_tells_model_no_action_was_executed(self):
        session = Mock()
        session.post.return_value.json.return_value = completion(action())
        choose_action(session, "test-key", "test-model", b"png", [], "Town", [], decision_error="Missing expected_result")
        content = session.post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("Missing expected_result", content[1]["text"])
        self.assertIn("NOT executed", content[1]["text"])

    def test_load_env_settings_and_preserve_shell_overrides(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            load_env(path)  # Missing .env is allowed when using shell variables.
            path.write_text(
                '# Settings\nOPENROUTER_API_KEY="test=key"\n'
                "OPENROUTER_MODEL='file/model'\nOTHER_SETTING=ignored\n",
                encoding="utf-8-sig",
            )
            os.environ["OPENROUTER_MODEL"] = "shell/model"
            load_env(path)
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "test=key")
            self.assertEqual(os.environ["OPENROUTER_MODEL"], "shell/model")
            self.assertNotIn("OTHER_SETTING", os.environ)

    def test_load_env_accepts_emulator_paths_without_unescaping_backslashes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text('SKYEMU_EXE="C:\\Emulators\\Sky Emu.exe"\nGAME_ROM=C:\\Games\\Pokemon.gba\nSKYEMU_SPEED=2\nSKYEMU_URL=http://localhost:8080\n')
            load_env(path)
            self.assertEqual(os.environ["SKYEMU_EXE"], r"C:\Emulators\Sky Emu.exe")
            self.assertEqual(os.environ["GAME_ROM"], r"C:\Games\Pokemon.gba")
            self.assertEqual(os.environ["SKYEMU_SPEED"], "2")

    def test_prepare_reuses_matching_game_without_reloading_or_launching(self):
        with tempfile.TemporaryDirectory() as directory, patch("play_pokemon.subprocess.Popen") as launch:
            rom = Path(directory) / "Pokemon.gba"
            rom.touch()
            emulator = SkyEmu("http://localhost:8080")
            emulator.api = Mock(return_value=Mock(text="pong\x00"))
            emulator.status = Mock(return_value={"rom-loaded": True, "rom-path": str(rom)})
            emulator.prepare("", str(rom), Path(directory))
            launch.assert_not_called()
            emulator.api.assert_called_once_with("ping", timeout=5)

    def test_prepare_loads_configured_rom_in_existing_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            rom = Path(directory) / "Pokemon.gba"
            rom.touch()
            emulator = SkyEmu("http://localhost:8080")
            emulator.api = Mock(side_effect=[Mock(text="pong\x00"), Mock(text="ok\x00")])
            emulator.status = Mock(side_effect=[{"rom-loaded": False}, {"rom-loaded": True}])
            emulator.prepare("", rom.name, Path(directory))
            emulator.api.assert_called_with("load_rom", path=str(rom.resolve()), pause=1)

    def test_prepare_launches_with_separate_arguments_and_waits(self):
        with tempfile.TemporaryDirectory() as directory, patch("play_pokemon.subprocess.Popen") as launch, patch("play_pokemon.time.sleep"):
            root = Path(directory)
            exe, rom = root / "Sky Emu.exe", root / "Pokemon Emerald.gba"
            exe.touch()
            rom.touch()
            launch.return_value.poll.return_value = None
            emulator = SkyEmu("http://localhost:8080")
            emulator.api = Mock(side_effect=[requests.ConnectionError(), requests.ConnectionError(), Mock(text="pong\x00")])
            emulator.status = Mock(return_value={"rom-loaded": True, "rom-path": str(rom)})
            emulator.prepare(exe.name, rom.name, root)
            launch.assert_called_once_with([str(exe.resolve()), str(rom.resolve())], cwd=str(root.resolve()))
            self.assertEqual(emulator.api.call_count, 3)

    def test_prepare_reports_startup_timeout(self):
        with tempfile.TemporaryDirectory() as directory, patch("play_pokemon.subprocess.Popen") as launch:
            root = Path(directory)
            (root / "SkyEmu.exe").touch()
            (root / "Pokemon.gba").touch()
            launch.return_value.poll.return_value = None
            emulator = SkyEmu("http://localhost:8080")
            emulator.api = Mock(side_effect=requests.ConnectionError())
            with self.assertRaisesRegex(ValueError, "Enable HTTP Control Server"):
                emulator.prepare("SkyEmu.exe", "Pokemon.gba", root, startup_timeout=0)

    def test_missing_rom_is_reported_before_launch(self):
        with tempfile.TemporaryDirectory() as directory, patch("play_pokemon.subprocess.Popen") as launch:
            emulator = SkyEmu("http://localhost:8080")
            with self.assertRaisesRegex(ValueError, "GAME_ROM is not a file"):
                emulator.prepare("missing.exe", "missing.gba", Path(directory))
            launch.assert_not_called()

    def test_windows_status_paths_are_repaired(self):
        emulator = SkyEmu("http://localhost:8080")
        emulator.api = Mock(return_value=Mock(text=r'{"rom-loaded":true,"rom-path":"C:\Users\Win10\Pokemon.gba","save-path":"C:\Users\Win10\Pokemon.sav","inputs":{"A":0}}'))
        status = emulator.status()
        self.assertEqual(status["rom-path"], r"C:\Users\Win10\Pokemon.gba")
        self.assertTrue(status["rom-loaded"])

    def test_status_accepts_null_terminated_json(self):
        emulator = SkyEmu("http://localhost:8080")
        emulator.api = Mock(return_value=Mock(text='{"rom-loaded":true,"inputs":{"A":0}}\x00\r\n'))
        self.assertTrue(emulator.status()["rom-loaded"])

    def test_reply_text_accepts_plain_and_null_terminated_responses(self):
        for body in ("pong", "pong\n", "pong\x00", " \tpong\x00\r\n"):
            with self.subTest(body=body):
                self.assertEqual(SkyEmu.reply_text(Mock(text=body)), "pong")

    def test_2x_speed_stays_enabled_during_actions_and_is_restored(self):
        emulator = SkyEmu("http://localhost:8080")
        emulator.api = Mock()
        emulator.status = Mock(return_value={"inputs": {"Emulator \uf04e": 0}})
        with patch("play_pokemon.time.sleep"):
            emulator.set_speed(2)
        self.assertEqual(emulator.speed, 2)
        emulator.api.assert_called_with("input", **{"Emulator \uf04e": 1})
        emulator.release()
        self.assertNotIn("Emulator \uf04e", emulator.api.call_args.kwargs)
        emulator.restore_speed()
        emulator.api.assert_called_with("input", **{"Emulator \uf04e": 0})
        self.assertEqual(emulator.speed, 1)

    def test_unsupported_speed_input_has_a_clear_error(self):
        emulator = SkyEmu("http://localhost:8080")
        emulator.status = Mock(return_value={"inputs": {"A": 0}})
        with self.assertRaisesRegex(ValueError, "SKYEMU_SPEED=1"):
            emulator.set_speed(2)

    def test_rejects_invalid_controller_actions(self):
        for invalid in (action("Reset Game"), action() | {"duration_ms": 500}, action() | {"duration_ms": True}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_action(invalid)

    def test_rejects_invalid_sequences_before_any_tap(self):
        for buttons in ([], ["A"] * 5, "A", ["Right", "Reset Game"], ["A", None]):
            with self.subTest(buttons=buttons), self.assertRaises(ValueError):
                emulator = SkyEmu("http://unused")
                emulator.api = Mock()
                try:
                    emulator.act(action() | {"buttons": buttons}, Operator("Play"), 0)
                finally:
                    emulator.api.assert_not_called()

    def test_sequence_preserves_order_and_releases_between_repeated_taps(self):
        emulator = SkyEmu("http://unused")
        clock = [0.0]
        calls = []

        def record(endpoint, **params):
            calls.append((clock[0], params))

        def advance(seconds):
            clock[0] += seconds

        emulator.api = record
        with patch("play_pokemon.time.monotonic", side_effect=lambda: clock[0]), patch("play_pokemon.time.sleep", side_effect=advance):
            execution = emulator.act(action(["Right", "Right", "Down", "A"]), Operator("Play"), 0)
        presses = [(stamp, next(b for b in BUTTONS if params[b])) for stamp, params in calls if any(params.values())]
        self.assertEqual([button for _, button in presses], ["Right", "Right", "Down", "A"])
        self.assertEqual(len(calls), 8)
        for index in (1, 3, 5, 7):
            self.assertEqual(calls[index][1], dict.fromkeys(BUTTONS, 0))
        for index in (2, 4, 6):
            self.assertGreaterEqual(calls[index][0] - calls[index - 1][0] + 1e-9, RELEASE_SECONDS)
        self.assertEqual(execution["completed_taps"], ["Right", "Right", "Down", "A"])
        self.assertTrue(execution["sequence_completed"])

    def test_instruction_or_pause_during_sequence_skips_remaining_taps(self):
        for instruction in ("/pause", "/quit", "Stop naming and wait"):
            with self.subTest(instruction=instruction):
                operator = Operator("Play")
                emulator = SkyEmu("http://unused")
                emulator.api = Mock()
                # Input arrives during the first tap; no later tap can run.
                with patch("play_pokemon.time.sleep", side_effect=lambda _: operator.receive(instruction)):
                    execution = emulator.act(action(["Right", "Down", "A"]), operator, 0)
                self.assertFalse(execution["sequence_completed"])
                self.assertEqual(execution["incomplete_tap"], "Right")
                self.assertEqual(execution["skipped_taps"], ["Down", "A"])
                self.assertEqual(execution["completed_taps"], [])
                self.assertEqual(emulator.api.call_count, 2)
                self.assertEqual(emulator.api.call_args.kwargs, dict.fromkeys(BUTTONS, 0))

    def test_pause_between_taps_preserves_completed_prefix(self):
        operator = Operator("Play")
        emulator = SkyEmu("http://unused")
        emulator.tap = Mock(return_value=True)
        emulator.wait_for_input = Mock(side_effect=lambda *_: operator.receive("/pause") or False)
        execution = emulator.act(action(["Right", "Right", "A"]), operator, 0)
        emulator.tap.assert_called_once_with("Right", operator, 0)
        self.assertEqual(execution["completed_taps"], ["Right"])
        self.assertEqual(execution["skipped_taps"], ["Right", "A"])
        self.assertIsNone(execution["incomplete_tap"])
        self.assertFalse(execution["sequence_completed"])

    def test_result_prints_previous_sequence_and_next_prediction_separately(self):
        decision = action(["Down", "A"]) | {
            "result": {"status": "no_change", "details": "The cursor is still on B."},
            "expected_result": "The next tap should move to H.",
        }
        output = format_decision(8, decision, {"decision": 7, "buttons": ["Right", "Right"]})
        self.assertIn("Result of [7] Right -> Right: [no_change] The cursor is still on B.", output)
        self.assertIn("Action: Down -> A", output)
        self.assertIn("Expect: The next tap should move to H.", output)
        self.assertIn("No previous sequence", format_decision(1, decision, None))

    def test_result_is_retained_with_the_previous_execution_record(self):
        next_decision = action() | {"result": {"status": "success", "details": "The cursor reached D."}}
        _, _, _, choose, _ = self.run_mock_game([action(["Right", "Right"]), next_decision])
        history = choose.call_args.args[6]
        self.assertEqual(history[0]["buttons"], ["Right", "Right"])
        self.assertEqual(history[0]["execution"]["completed_taps"], ["Right", "Right"])
        self.assertEqual(history[0]["observed_result"], next_decision["result"])

    def test_rejects_missing_or_invalid_result_fields(self):
        for result in (None, {}, {"status": "success", "details": ""}, {"status": "invented", "details": "Unknown"}):
            with self.subTest(result=result), self.assertRaises(ModelDecisionError):
                parse_model_action(completion(action() | {"result": result}))

    def test_instruction_change_discards_pending_action(self):
        operator = Operator("Play")
        version = operator.snapshot()[0]
        operator.receive("Catch Pikachu")
        emulator = SkyEmu("http://unused")
        emulator.api = Mock()
        self.assertFalse(emulator.tap("A", operator, version))
        emulator.api.assert_called_once_with("input", **dict.fromkeys(BUTTONS, 0))

    def test_releases_buttons_if_press_request_fails(self):
        emulator = SkyEmu("http://unused")
        emulator.api = Mock(side_effect=[requests.ConnectionError("lost response"), None])
        with self.assertRaises(requests.ConnectionError):
            emulator.tap("A", Operator("Play"), 0)
        self.assertEqual(emulator.api.call_args.kwargs, dict.fromkeys(BUTTONS, 0))

    def test_pause_interrupts_a_tap(self):
        operator = Operator("Play")
        emulator = SkyEmu("http://unused")
        emulator.api = Mock()
        with patch("play_pokemon.time.sleep", side_effect=lambda _: operator.receive("/pause")):
            self.assertFalse(emulator.tap("A", operator, 0))
        self.assertEqual(emulator.api.call_args.kwargs, dict.fromkeys(BUTTONS, 0))

    def test_one_tap_is_released_after_fixed_time_at_each_speed(self):
        for speed in (1, 2):
            with self.subTest(speed=speed):
                clock = [0.0]
                emulator = SkyEmu("http://unused")
                emulator.speed = speed
                emulator.api = Mock()

                def advance(seconds):
                    clock[0] += seconds

                with patch("play_pokemon.time.monotonic", side_effect=lambda: clock[0]), patch("play_pokemon.time.sleep", side_effect=advance):
                    self.assertTrue(emulator.tap("Down", Operator("Play"), 0))
                self.assertAlmostEqual(clock[0], TAP_SECONDS / speed)
                self.assertEqual(emulator.api.call_count, 2)
                self.assertEqual(emulator.api.call_args_list[0].kwargs["Down"], 1)
                self.assertEqual(emulator.api.call_args_list[1].kwargs, dict.fromkeys(BUTTONS, 0))

    def test_wait_does_not_press_a_button(self):
        emulator = SkyEmu("http://unused")
        emulator.api = Mock()
        self.assertTrue(emulator.tap("WAIT", Operator("Play"), 0))
        for call in emulator.api.call_args_list:
            self.assertEqual(call.kwargs, dict.fromkeys(BUTTONS, 0))

    def test_model_receives_image_and_operator_instructions(self):
        session = Mock()
        session.post.return_value.json.return_value = completion(action())
        chosen = choose_action(session, "test-key", "test-model", b"png", ["Catch Pikachu"], "Town", [])
        self.assertEqual(chosen["buttons"], ["A"])
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(session.post.call_args.args[0], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(session.post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")
        content = payload["messages"][1]["content"]
        self.assertIn("Catch Pikachu", content[0]["text"])
        self.assertEqual(content[-1]["image_url"]["url"], "data:image/png;base64," + base64.b64encode(b"png").decode())
        self.assertEqual(payload["tools"][0]["function"]["name"], "press_buttons")
        self.assertNotIn("duration_ms", payload["tools"][0]["function"]["parameters"]["properties"])
        self.assertNotIn("parallel_tool_calls", payload)
        self.assertEqual(payload["provider"], {"require_parameters": True})
        self.assertEqual(payload["tool_choice"], "auto")

    def test_haiku_uses_auto_tool_selection_with_validated_taps(self):
        session = Mock()
        session.post.return_value.json.return_value = completion(action(["Left", "A"]))
        chosen = choose_action(session, "test-key", "anthropic/claude-haiku-5.5", b"png", ["Play"], "", [])
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "anthropic/claude-haiku-5.5")
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(chosen["buttons"], ["Left", "A"])

    def test_plain_text_with_auto_selection_is_rejected_for_correction(self):
        session = Mock()
        session.post.return_value.json.return_value = {"choices": [{
            "finish_reason": "stop", "message": {"content": "I would press A."}
        }]}
        with self.assertRaisesRegex(ModelDecisionError, "press_buttons tool call"):
            choose_action(session, "test-key", "anthropic/claude-haiku-5.5", b"png", [], "", [])

    def test_previous_and_current_images_are_labeled_and_ordered(self):
        session = Mock()
        session.post.return_value.json.return_value = completion(action())
        prior = dict(action(), expected_result="Cursor moves to B")
        choose_action(session, "test-key", "test-model", b"current", [], "Name chosen: NOVA", [prior], previous_image=b"previous")
        content = session.post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertIn("Cursor moves to B", content[0]["text"])
        self.assertIn("Name chosen: NOVA", content[0]["text"])
        self.assertIn("PREVIOUS", content[1]["text"])
        self.assertTrue(content[2]["image_url"]["url"].endswith(base64.b64encode(b"previous").decode()))
        self.assertIn("CURRENT", content[3]["text"])
        self.assertTrue(content[4]["image_url"]["url"].endswith(base64.b64encode(b"current").decode()))

    def test_rejects_actions_without_a_plan_or_expected_outcome(self):
        for change in ({"plan": []}, {"plan": [""]}, {"objective": ""}, {"expected_result": ""}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_action(action() | change)

    def test_http_error_shows_provider_message_and_model_without_key(self):
        session = Mock()
        response = requests.Response()
        response.status_code = 404
        response._content = b'{"error":{"message":"No endpoints found supporting parameters. test-secret"}}'
        session.post.return_value = response
        with self.assertRaises(ValueError) as raised:
            choose_action(session, "test-secret", "google/test-model:free", b"png", [], "", [])
        message = str(raised.exception)
        self.assertIn("HTTP 404", message)
        self.assertIn("google/test-model:free", message)
        self.assertIn("No endpoints found supporting parameters", message)
        self.assertNotIn("test-secret", message)

    def test_non_json_http_error_is_readable(self):
        session = Mock()
        response = requests.Response()
        response.status_code = 502
        response._content = b'<html>Bad gateway</html>'
        session.post.return_value = response
        with self.assertRaisesRegex(ValueError, "HTTP 502.*non-JSON"):
            choose_action(session, "test-key", "test-model", b"png", [], "", [])

    def test_refusal_never_becomes_an_action(self):
        session = Mock()
        session.post.return_value.json.return_value = completion(finish_reason="stop")
        with self.assertRaises(ValueError):
            choose_action(session, "test-key", "test-model", b"png", [], "", [])

    def test_rejects_multiple_or_truncated_actions_and_provider_errors(self):
        session = Mock()
        for response in (completion(action(), action()), completion(action(), finish_reason="length"),
                         {"error": {"message": "Provider unavailable"}}, {"choices": []}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                session.post.return_value.json.return_value = response
                choose_action(session, "test-key", "test-model", b"png", [], "", [])

    def test_model_button_validation_is_enforced_for_openrouter(self):
        session = Mock()
        session.post.return_value.json.return_value = completion(action("Reset Game"))
        with self.assertRaises(ValueError):
            choose_action(session, "test-key", "test-model", b"png", [], "", [])


if __name__ == "__main__":
    unittest.main()
