import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from play_pokemon import ActivityLog, BUTTONS, TAP_SECONDS, RELEASE_SECONDS, ModelDecisionError, Operator, SkyEmu, choose_action, format_decision, load_env, main, parse_model_action, request_completion, validate_action, verified_request


def action(button="A"):
    return {"operator_response": "", "buttons": button if isinstance(button, list) else [button], "reason": "Advance dialogue", "memory": "In dialogue",
            "memory_update": {"current_location": "unknown",
                              "goal_hierarchy": {"main_goal": "Complete Pokemon Emerald", "subgoal": "Begin the adventure", "task": "Finish the introduction"},
                              "events": [], "observed_facts": [], "hypotheses": [], "forget_knowledge": []},
            "result": {"status": "uncertain", "details": "The previous outcome is not yet clear."},
            "observation": "Dialogue page with a next indicator", "objective": "Finish the introduction",
            "plan": ["Advance this page", "Read the next page"], "expected_result": "A new dialogue page"}


def completion(*actions, finish_reason="tool_calls"):
    return {"choices": [{"finish_reason": finish_reason, "message": {"tool_calls": [
        {"type": "function", "function": {"name": "press_buttons", "arguments": json.dumps(item)}}
        for item in actions
    ]}}]}


class HarnessTests(unittest.TestCase):
    def test_codex_only_startup_does_not_require_openrouter_key(self):
        result, _, _, choose, _ = self.run_mock_game([action()],
            environment={"MAIN_MODEL": "codex/gpt-6.1-sol/high"})
        self.assertEqual(result, 0)
        self.assertEqual(choose.call_args.args[1:3], (None, "codex/gpt-6.1-sol/high"))

    def test_codex_startup_rejects_invalid_configuration_before_emulator_control(self):
        for extra in ({"SUBMODEL": "provider/navigation"}, {"DECISION_MODEL": "provider/router"},
                      {"MAIN_MODEL": "codex/model"}, {"DECISION_MODEL": "codex/model/high"}):
            with self.subTest(extra=extra), patch.dict(os.environ, {"MAIN_MODEL": "codex/gpt-6.1-sol/high", **extra}, clear=True), \
                 patch("sys.argv", ["play_pokemon.py"]), patch("play_pokemon.load_env"), \
                 patch("sys.stderr"), patch("play_pokemon.SkyEmu") as emulator:
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)
                emulator.assert_not_called()

    def test_codex_transport_uses_existing_validation_and_reports_usage(self):
        session, observer = Mock(), Mock()
        response = completion(action()) | {"model": "codex/gpt-6.1-sol/high", "usage": {"input_tokens": 12}}
        with patch("play_pokemon.request_codex", return_value=response) as codex:
            chosen = choose_action(session, None, "codex/gpt-6.1-sol/high", b"png", ["Play"], {}, [], observer=observer)
        self.assertEqual(chosen["buttons"], ["A"])
        self.assertEqual(codex.call_args.args[0]["model"], "codex/gpt-6.1-sol/high")
        session.post.assert_not_called()
        self.assertEqual(observer.call_args.args, ("response", response))

    def test_codex_submodel_can_receive_openrouter_router_handoff(self):
        session = Mock()
        session.post.return_value.json.return_value = self.router_reply("SUBMODEL")
        reply = action("Right") | {"navigation_status": "continue"}
        memory = self.router_memory()
        memory["working_memory"]["navigation_task"] = self.navigation_task()
        with patch("play_pokemon.request_codex", return_value=completion(reply)) as codex:
            chosen = choose_action(session, "key", "main", b"png", ["Play"], memory,
                [action()], decision_model="router", submodel="codex/gpt-6.1-sol/high")
        self.assertEqual(chosen["buttons"], ["Right"])
        self.assertEqual(codex.call_args.args[0]["model"], "codex/gpt-6.1-sol/high")
        self.assertEqual(session.post.call_count, 1)

    def test_activity_records_each_query_before_decision_validation_and_appends_across_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs" / "usage.json"
            session = Mock()
            router = self.router_reply("MODEL") | {"usage": {"prompt_tokens": 12, "completion_tokens": 1}}
            invalid = {"model": "provider/actual", "usage": {"prompt_tokens": 100, "completion_tokens": 25}, "choices": []}
            session.post.side_effect = [Mock(json=Mock(return_value=router)), Mock(json=Mock(return_value=invalid))]
            with self.assertRaises(ModelDecisionError):
                choose_action(session, "secret-key", "main", b"png", ["Play"], self.router_memory(),
                              [action()], decision_model="router", observer=ActivityLog(path))
            entries = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([(entry["model"], entry["input_tokens"], entry["output_tokens"]) for entry in entries],
                             [("router", 12, 1), ("provider/actual", 100, 25)])
            self.assertTrue(all(entry["timestamp"].endswith("+00:00") for entry in entries))
            self.assertNotIn("secret-key", path.read_text(encoding="utf-8"))
            session.post.side_effect = None
            session.post.return_value = Mock(json=Mock(return_value={"usage": {"input_tokens": 9, "output_tokens": 0}}))
            request_completion(session, "secret-key", {"model": "navigation"}, observer=ActivityLog(path))
            entries = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(entries), 3)
            self.assertEqual(entries[-1]["model"], "navigation")
            self.assertEqual(entries[-1]["input_tokens"], 9)
            self.assertEqual(entries[-1]["output_tokens"], 0)

    def test_activity_missing_usage_and_transport_failures_record_unknown_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            logger = ActivityLog(path)
            session = Mock()
            session.post.return_value = Mock(json=Mock(return_value={"usage": None}))
            request_completion(session, "key", {"model": "main"}, observer=logger)
            session.post.side_effect = requests.Timeout("timed out")
            with self.assertRaises(requests.Timeout):
                request_completion(session, "key", {"model": "router"}, observer=logger)
            entries = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([entry["model"] for entry in entries], ["main", "router"])
            self.assertTrue(all(entry["input_tokens"] is None and entry["output_tokens"] is None for entry in entries))

    def test_activity_preserves_invalid_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            for content in ("{broken", "{}"):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "usage.json"):
                        ActivityLog(path)
                    self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_activity_initializes_empty_and_whitespace_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            for content in ("", " \n\t", "\ufeff", "\ufeff[]"):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    logger = ActivityLog(path)
                    self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])
                    logger("request", {"model": "main"})
                    logger("response", {"usage": {"prompt_tokens": 2, "completion_tokens": 1}})
                    self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))), 1)

    def test_activity_cli_defaults_to_activity_json_and_is_disabled_without_flag(self):
        with patch("play_pokemon.ActivityLog") as logger:
            result, _, _, choose, _ = self.run_mock_game([action()])
            self.assertEqual(result, 0)
            logger.assert_not_called()
            self.assertNotIn("observer", choose.call_args.kwargs)
            result, _, _, choose, _ = self.run_mock_game([action()], ["--activity"])
            self.assertEqual(result, 0)
            logger.assert_called_once_with(Path("activity.json"))
            self.assertTrue(callable(choose.call_args.kwargs["observer"]))

    def test_activity_cli_records_usage_with_and_without_dashboard(self):
        for mode in ("--terminal", "--browser"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "usage.json"
                payload = {"model": "actual/model", "usage": {"prompt_tokens": 7, "completion_tokens": 3}}

                def decide(*args, observer, **kwargs):
                    observer("request", {"model": "requested/model"})
                    observer("response", payload)
                    return action()

                with patch("pokemon_web.Dashboard") as dashboard:
                    result, _, _, _, _ = self.run_mock_game(decide, [mode, "--activity", str(path)], steps=1)
                self.assertEqual(result, 0)
                entry, = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual((entry["model"], entry["input_tokens"], entry["output_tokens"]), ("actual/model", 7, 3))
                if mode == "--browser":
                    self.assertEqual(dashboard.return_value.record_model.call_count, 2)
                    dashboard.return_value.record_model.assert_called_with("response", payload)

    def router_memory(self):
        from game_memory import GameMemory
        with tempfile.TemporaryDirectory() as directory:
            store = GameMemory(Path(directory), "Pokemon Emerald", "Complete Pokemon Emerald")
            store.accept(action())
            return store.context()

    def router_reply(self, route, answer_type="choice"):
        return {"answers": {"route": {"type": answer_type, "choice": route,
                                     "probabilities": {"WAIT": 0.1, "ADVANCE": 0.1, "MODEL": 0.8}}}}

    def test_router_shortcuts_wait_and_advance_preserve_planner_memory(self):
        for route, buttons in (("WAIT", ["WAIT"]), ("ADVANCE", ["A"])):
            with self.subTest(route=route):
                memory = self.router_memory()
                session = Mock()
                session.post.return_value.json.return_value = self.router_reply(route)
                chosen = choose_action(session, "key", "main", b"current", ["Play"], memory,
                                       [action()], b"previous", decision_model="router")
                session.post.assert_called_once()
                self.assertEqual(session.post.call_args.args[0], "https://openrouter.ai/api/alpha/decisions")
                payload = session.post.call_args.kwargs["json"]
                self.assertEqual(set(payload["questions"]["route"]["criteria"]), {"WAIT", "ADVANCE", "SUBMODEL", "MODEL"})
                self.assertEqual(chosen["buttons"], buttons)
                self.assertEqual(chosen["memory"], memory["working_memory"]["notes"])
                self.assertEqual(chosen["memory_update"]["goal_hierarchy"], memory["working_memory"]["goal_hierarchy"])
                self.assertEqual(chosen["memory_update"]["events"], [])
                self.assertEqual(chosen["result"]["status"], "uncertain")
                validate_action(chosen)

    def test_router_model_forwards_identical_input_and_reports_both_exchanges(self):
        session = Mock()
        events = []
        session.post.side_effect = [Mock(json=Mock(return_value=self.router_reply("MODEL"))),
                                    Mock(json=Mock(return_value=completion(action())))]
        chosen = choose_action(session, "secret", "main", b"current", ["Play"], self.router_memory(),
                               [action()], b"previous", soul="Choose Mudkip", decision_model="router",
                               observer=lambda kind, payload: events.append((kind, payload)))
        self.assertEqual(chosen["buttons"], ["A"])
        router, planner = [call.kwargs["json"] for call in session.post.call_args_list]
        self.assertEqual(router["model"], "router")
        self.assertEqual(planner["model"], "main")
        self.assertEqual(router["state"], planner["messages"][1]["content"])
        self.assertIn("Choose Mudkip", router["questions"]["route"]["instructions"])
        self.assertEqual([kind for kind, _ in events], ["request", "response", "request", "response"])
        self.assertNotIn("secret", json.dumps(events))

    def test_router_cannot_bypass_initial_plan_unanswered_messages_or_corrections(self):
        for history, messages, error in (([], ["Play"], ""),
                                          ([action()], ["Play", "Stop advancing"], ""),
                                          ([action()], ["Play"], "Invalid action")):
            with self.subTest(messages=messages, error=error, history=history):
                session = Mock()
                answer = action() | {"operator_response": "I will stop advancing."}
                session.post.side_effect = [Mock(json=Mock(return_value=self.router_reply("ADVANCE"))),
                                            Mock(json=Mock(return_value=completion(answer)))]
                choose_action(session, "key", "main", b"png", messages, self.router_memory(), history,
                              decision_error=error, decision_model="router")
                self.assertEqual(session.post.call_count, 2)

    def test_invalid_router_output_falls_back_to_main_without_guessing_buttons(self):
        for reply in (self.router_reply("A"), self.router_reply("ADVANCE", "noul"),
                      self.router_reply(None), {"answers": {}}):
            with self.subTest(reply=reply):
                session = Mock()
                session.post.side_effect = [Mock(json=Mock(return_value=reply)),
                                            Mock(json=Mock(return_value=completion(action("Left"))))]
                chosen = choose_action(session, "key", "main", b"png", ["Play"], self.router_memory(),
                                       [action()], decision_model="router")
                self.assertEqual(chosen["buttons"], ["Left"])
                self.assertEqual(session.post.call_count, 2)

    def test_decision_model_env_and_cli_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "router.env"
            path.write_text('OPENROUTER_API_KEY=key\nMAIN_MODEL=main\nDECISION_MODEL=file/router\n', encoding="utf-8")
            for overrides, environment, expected in (([], {}, "file/router"),
                ([], {"DECISION_MODEL": "shell/router"}, "shell/router"),
                (["--decision-model", "cli/router"], {}, "cli/router"),
                (["--decision-model", ""], {}, "")):
                with self.subTest(expected=expected):
                    result, _, _, choose, _ = self.run_mock_game([action()],
                        ["--env-file", str(path), *overrides], environment=environment, load_settings=True)
                    self.assertEqual(result, 0)
                    self.assertEqual(choose.call_args.kwargs["decision_model"], expected)

    def test_create_env_saves_decision_model_and_can_disable_it(self):
        import create_env
        for router in ("perplexity/pplx-decider-v1.2", ""):
            with self.subTest(router=router), tempfile.TemporaryDirectory() as directory:
                script = Path(directory) / "create_env.py"
                with patch.object(create_env, "__file__", str(script)), \
                     patch.object(create_env, "getpass", return_value="test-key"), \
                     patch("builtins.input", side_effect=["main/model", router, "navigation/model", "", "", "", ""]), \
                     patch("builtins.print"):
                    create_env.main()
                with patch.dict(os.environ, {}, clear=True):
                    load_env(script.with_name(".env"))
                    self.assertEqual(os.environ["DECISION_MODEL"], router)
                    self.assertEqual(os.environ["SUBMODEL"], "navigation/model")

    def navigation_task(self):
        return {"destination": "Oldale Town", "instructions": "Follow the northern path on Route 101.",
                "arrival_condition": "The Oldale Town sign appears.", "constraints": "Avoid optional interactions; stop for battles."}

    def test_submodel_receives_assignment_and_cannot_replace_it(self):
        memory = self.router_memory()
        memory["working_memory"]["navigation_task"] = self.navigation_task()
        sub_action = action("Up") | {"navigation_status": "continue", "navigation_task": None}
        session = Mock()
        session.post.side_effect = [Mock(json=Mock(return_value=self.router_reply("SUBMODEL"))),
                                    Mock(json=Mock(return_value=completion(sub_action)))]
        chosen = choose_action(session, "key", "main", b"png", ["Play"], memory, [action()],
                               decision_model="router", submodel="cheap/navigation")
        self.assertEqual(chosen["buttons"], ["Up"])
        self.assertNotIn("navigation_task", chosen)
        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "cheap/navigation")
        self.assertIn("Oldale Town", payload["messages"][1]["content"][0]["text"])
        self.assertIn("navigation submodel", payload["messages"][0]["content"])
        self.assertEqual(payload["tools"][0]["function"]["parameters"]["properties"]["buttons"]["items"]["enum"],
                         ["Up", "Down", "Left", "Right", "WAIT"])

    def test_submodel_route_requires_configuration_and_assignment(self):
        for configured, assigned in ((False, True), (True, False)):
            with self.subTest(configured=configured, assigned=assigned):
                memory = self.router_memory()
                if assigned:
                    memory["working_memory"]["navigation_task"] = self.navigation_task()
                session = Mock()
                session.post.side_effect = [Mock(json=Mock(return_value=self.router_reply("SUBMODEL"))),
                    Mock(json=Mock(return_value=completion(action() | {"navigation_task": None})))]
                choose_action(session, "key", "main", b"png", ["Play"], memory, [action()],
                              decision_model="router", submodel="cheap" if configured else "")
                self.assertEqual(session.post.call_args.kwargs["json"]["model"], "main")

    def test_submodel_cannot_bypass_first_turn_operator_messages_or_corrections(self):
        for history, messages, error in (([], ["Play"], ""),
            ([action()], ["Play", "Change destination"], ""), ([action()], ["Play"], "Invalid reply")):
            with self.subTest(messages=messages, error=error, history=history):
                memory = self.router_memory()
                memory["working_memory"]["navigation_task"] = self.navigation_task()
                session = Mock()
                main_action = action("WAIT") | {"navigation_task": None, "operator_response": "I will reassess."}
                session.post.side_effect = [Mock(json=Mock(return_value=self.router_reply("SUBMODEL"))),
                    Mock(json=Mock(return_value=completion(main_action)))]
                chosen = choose_action(session, "key", "main", b"png", messages, memory, history,
                                       decision_error=error, decision_model="router", submodel="cheap")
                self.assertEqual(chosen, main_action)
                self.assertEqual(session.post.call_args.kwargs["json"]["model"], "main")

    def test_invalid_navigation_assignments_are_rejected(self):
        for task in ({}, {"destination": "Oldale"}, self.navigation_task() | {"arrival_condition": ""},
                     self.navigation_task() | {"instructions": 3}):
            with self.subTest(task=task), self.assertRaisesRegex(ModelDecisionError, "navigation_task"):
                parse_model_action(completion(action() | {"navigation_task": task}))

    def test_submodel_hands_back_arrival_blockage_invalid_actions_and_goal_changes(self):
        for change in ({"navigation_status": "completed"}, {"navigation_status": "blocked"},
                       {"buttons": ["A"]}, {"expected_result": ""},
                       {"memory_update": action()["memory_update"] | {"goal_hierarchy": {
                           "main_goal": "Different goal", "subgoal": "Different", "task": "Different"}}}):
            with self.subTest(change=change):
                memory = self.router_memory()
                memory["working_memory"]["navigation_task"] = self.navigation_task()
                sub_action = action("Up") | {"navigation_status": "continue"} | change
                main_action = action("WAIT") | {"navigation_task": None}
                session = Mock()
                session.post.side_effect = [Mock(json=Mock(return_value=self.router_reply("SUBMODEL"))),
                    Mock(json=Mock(return_value=completion(sub_action))),
                    Mock(json=Mock(return_value=completion(main_action)))]
                chosen = choose_action(session, "key", "main", b"png", ["Play"], memory, [action()],
                                       decision_model="router", submodel="cheap")
                self.assertEqual(chosen, main_action)
                self.assertEqual([call.kwargs["json"]["model"] for call in session.post.call_args_list],
                                 ["router", "cheap", "main"])
                self.assertIn("NOT executed", session.post.call_args.kwargs["json"]["messages"][1]["content"][-1]["text"])

    def test_main_model_must_supply_explicit_delegation_or_null(self):
        session = Mock()
        session.post.return_value.json.return_value = completion(action())
        with self.assertRaisesRegex(ModelDecisionError, "navigation_task"):
            choose_action(session, "key", "main", b"png", ["Play"], self.router_memory(), [], submodel="cheap")
        session.post.return_value.json.return_value = completion(action() | {"navigation_task": self.navigation_task()})
        chosen = choose_action(session, "key", "main", b"png", ["Play"], self.router_memory(), [], submodel="cheap")
        self.assertEqual(chosen["navigation_task"], self.navigation_task())

    def test_main_movement_requires_delegation_or_specific_retention_reason(self):
        session = Mock()
        session.post.return_value.json.return_value = completion(action("Up") | {"navigation_task": None})
        with self.assertRaisesRegex(ModelDecisionError, "navigation_hold_reason"):
            choose_action(session, "key", "main", b"png", ["Play"], self.router_memory(), [], submodel="cheap")
        session.post.return_value.json.return_value = completion(action("Up") | {
            "navigation_task": None, "navigation_hold_reason": "Selecting a battle-menu item, not walking."})
        chosen = choose_action(session, "key", "main", b"png", ["Play"], self.router_memory(), [], submodel="cheap")
        self.assertEqual(chosen["buttons"], ["Up"])
        session.post.return_value.json.return_value = completion(action("Right") | {"navigation_task": self.navigation_task()})
        chosen = choose_action(session, "key", "main", b"png", ["Play"], self.router_memory(), [], submodel="cheap")
        self.assertEqual(chosen["navigation_task"], self.navigation_task())

    def test_routing_override_explains_missing_navigation_assignment(self):
        session = Mock()
        session.post.side_effect = [Mock(json=Mock(return_value=self.router_reply("SUBMODEL"))),
            Mock(json=Mock(return_value=completion(action() | {"navigation_task": self.navigation_task()})))]
        with patch("builtins.print") as output:
            choose_action(session, "key", "main", b"png", ["Play"], self.router_memory(), [action()],
                          decision_model="router", submodel="cheap")
        lines = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("SUBMODEL -> MODEL", lines)
        self.assertIn("has not saved a navigation_task", lines)

    def test_delegation_assignment_is_used_on_next_turn(self):
        from game_memory import GameMemory
        with tempfile.TemporaryDirectory() as directory:
            store = GameMemory(directory, "Pokemon Emerald", "Complete the game")
            assigned = action("Up") | {"navigation_task": self.navigation_task()}
            session = Mock()
            session.post.side_effect = [Mock(json=Mock(return_value=self.router_reply("MODEL"))),
                Mock(json=Mock(return_value=completion(assigned)))]
            chosen = choose_action(session, "key", "main", b"png", ["Play"], store.context(), [],
                                   decision_model="router", submodel="cheap")
            store.accept(chosen)
            session.post.side_effect = [Mock(json=Mock(return_value=self.router_reply("SUBMODEL"))),
                Mock(json=Mock(return_value=completion(action("Up") | {"navigation_status": "continue"})))]
            chosen = choose_action(session, "key", "main", b"next", ["Play"], store.context(), [assigned],
                                   decision_model="router", submodel="cheap")
            self.assertEqual(session.post.call_args.kwargs["json"]["model"], "cheap")
            self.assertEqual(chosen["buttons"], ["Up"])

    def test_submodel_env_and_cli_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submodel.env"
            path.write_text('OPENROUTER_API_KEY=key\nMAIN_MODEL=main\nSUBMODEL=file/sub\n', encoding="utf-8")
            for overrides, environment, expected in (([], {}, "file/sub"),
                ([], {"SUBMODEL": "shell/sub"}, "shell/sub"),
                (["--submodel", "cli/sub"], {}, "cli/sub"), (["--submodel", ""], {}, "")):
                with self.subTest(expected=expected):
                    result, _, _, choose, _ = self.run_mock_game([action()],
                        ["--env-file", str(path), *overrides], environment=environment, load_settings=True)
                    self.assertEqual(result, 0)
                    self.assertEqual(choose.call_args.kwargs["submodel"], expected)

    def test_dialogue_shortcuts_run_through_execution_and_history(self):
        from play_pokemon import dialogue_action
        waiting = dialogue_action("WAIT", self.router_memory())
        advancing = dialogue_action("ADVANCE", self.router_memory())
        result, emulator, _, choose, frames = self.run_mock_game([action(), waiting, advancing, action()])
        self.assertEqual(result, 0)
        self.assertEqual([call.args[0]["buttons"] for call in emulator.act.call_args_list],
                         [["A"], ["WAIT"], ["A"], ["A"]])
        history = choose.call_args_list[-1].args[6]
        self.assertEqual(history[1]["buttons"], ["WAIT"])
        self.assertEqual(history[2]["buttons"], ["A"])
        self.assertEqual(choose.call_args_list[-1].args[7], frames[-2])

    def test_quiet_turns_do_not_trigger_a_speaking_quota(self):
        session = Mock()
        session.post.return_value.json.return_value = completion(action())
        for quiet_turns in (0, 9, 10, 20):
            with self.subTest(quiet_turns=quiet_turns):
                chosen = choose_action(session, "key", "model", b"png", ["Play"], "Continuing the same plan",
                                       [action() for _ in range(quiet_turns)])
                self.assertEqual(chosen["operator_response"], "")
                self.assertNotIn("commentary", chosen)
                context = json.loads(session.post.call_args.kwargs["json"]["messages"][1]["content"][0]["text"])
                self.assertNotIn("commentary_schedule", context)

    def test_punctuation_only_player_output_is_silent(self):
        for placeholder in ('>', '">"', "'>'", ' >> ', '...', '…', '""'):
            with self.subTest(placeholder=placeholder):
                decision = action() | {"operator_response": placeholder, "commentary": placeholder}
                parsed = parse_model_action(completion(decision))
                self.assertEqual(parsed["operator_response"], "")
                self.assertEqual(parsed["commentary"], "")
                output = format_decision(1, decision, None)
                self.assertNotIn("Player:", output)
                self.assertNotIn("Thoughts:", output)

    def test_placeholder_cannot_count_as_an_operator_answer(self):
        session = Mock()
        session.post.return_value.json.return_value = completion(
            action() | {"operator_response": '">"', "commentary": '...'})
        with self.assertRaisesRegex(ModelDecisionError, "Reply directly"):
            choose_action(session, "key", "model", b"png", ["Play", "What next?"], "", [])

    def test_real_player_text_preserves_symbols_and_other_languages(self):
        for text in ('HP > 10, so we can keep exploring.', '좋아! 다음 체육관을 준비하자.', 'Level 5!'):
            with self.subTest(text=text):
                parsed = parse_model_action(completion(action() | {"commentary": text}))
                self.assertEqual(parsed["commentary"], text)

    def test_commentary_is_optional_and_printed_only_when_nonempty(self):
        for value in (None, "", "   "):
            decision = action() if value is None else action() | {"commentary": value}
            self.assertNotIn("Thoughts:", format_decision(1, parse_model_action(completion(decision)), None))
        thought = "I'm worried about the next gym. I'll train a balanced team before challenging it."
        decision = action() | {"commentary": thought}
        self.assertIn("Thoughts: " + thought, format_decision(1, decision, None))
        _, _, _, choose, _ = self.run_mock_game([decision, action()])
        self.assertEqual(choose.call_args.args[6][0]["commentary"], thought)

    def test_commentary_can_answer_operator_without_duplicate_reply(self):
        reply = "I'll check the summary to confirm its moves before we continue."
        session = Mock()
        session.post.return_value.json.return_value = completion(action("Start") | {"commentary": reply})
        chosen = choose_action(session, "key", "model", b"png", ["Play", "What are its moves?"], "", [])
        output = format_decision(1, chosen, None)
        self.assertEqual(output.count(reply), 1)
        chosen["operator_response"] = reply
        self.assertEqual(format_decision(1, chosen, None).count(reply), 1)

    def test_invalid_commentary_is_rejected(self):
        for value in (None, [], 5):
            with self.subTest(value=value), self.assertRaisesRegex(ModelDecisionError, "commentary"):
                parse_model_action(completion(action() | {"commentary": value}))

    def test_unanswered_question_requires_a_direct_reply(self):
        session = Mock()
        session.post.return_value.json.return_value = completion(action())
        with self.assertRaisesRegex(ModelDecisionError, "Reply directly"):
            choose_action(session, "key", "model", b"png",
                          ["Play", "What are your starter's moves?"], "Continue the story", [])
        payload = session.post.call_args.kwargs["json"]
        context = json.loads(payload["messages"][1]["content"][0]["text"])
        self.assertEqual(context["unanswered_operator_messages"], ["What are your starter's moves?"])

    def test_reply_is_visible_and_retained_without_repeated_acknowledgments(self):
        reply = "I haven't checked its moves yet. I'll open its summary now."
        decision = action("Start") | {"operator_response": reply}
        session = Mock()
        session.post.return_value.json.return_value = completion(decision)
        messages = ["Play", "What are its moves?"]
        chosen = choose_action(session, "key", "model", b"png", messages, "", [])
        self.assertIn("Player: " + reply, format_decision(1, chosen, None))
        _, _, _, choose, _ = self.run_mock_game([decision, action()])
        self.assertEqual(choose.call_args.args[6][0]["operator_response"], reply)
        session.post.return_value.json.return_value = completion(action())
        choose_action(session, "key", "model", b"png", messages, "", [], replied_messages=2)
        context = json.loads(session.post.call_args.kwargs["json"]["messages"][1]["content"][0]["text"])
        self.assertEqual(context["unanswered_operator_messages"], [])

    def test_missing_reply_field_is_rejected(self):
        decision = action()
        del decision["operator_response"]
        with self.assertRaisesRegex(ModelDecisionError, "operator_response"):
            parse_model_action(completion(decision))

    def test_discarded_inference_does_not_mark_new_messages_replied(self):
        operator = Operator("Play")

        def decisions():
            operator.receive("What are its moves?")
            yield action() | {"operator_response": "Stale reply"}
            yield action("Start") | {"operator_response": "I'll check its summary now."}
            yield action()

        with patch("builtins.print") as output:
            result, emulator, _, choose, _ = self.run_mock_game(decisions(), operator=operator, steps=3)
        self.assertEqual(result, 0)
        self.assertEqual(emulator.act.call_count, 2)
        self.assertEqual([call.kwargs["replied_messages"] for call in choose.call_args_list], [1, 1, 2])
        self.assertNotIn("Stale reply", str(output.call_args_list))

    def test_soul_is_included_with_controller_rules_in_model_request(self):
        session = Mock()
        session.post.return_value.json.return_value = completion(action())
        choose_action(session, "test-key", "test-model", b"png", ["Choose Mudkip"], "", [],
                      soul="Use trainer name NOVA. Choose Torchic.")
        messages = session.post.call_args.kwargs["json"]["messages"]
        self.assertIn("Use trainer name NOVA. Choose Torchic.", messages[0]["content"])
        self.assertIn("call press_buttons exactly once", messages[0]["content"])
        self.assertIn("Session operator instructions take priority", messages[0]["content"])
        self.assertIn("Choose Mudkip", messages[1]["content"][0]["text"])

    def test_soul_file_setting_and_cli_override(self):
        with tempfile.TemporaryDirectory() as directory:
            configured = Path(directory) / "trainer soul.md"
            configured.write_text("Choose Torchic. 트레이너 NOVA", encoding="utf-8-sig")
            override = Path(directory) / "other.md"
            override.write_text("Choose Mudkip.", encoding="utf-8")
            settings = Path(directory) / "settings.env"
            settings.write_text(f'SOUL_FILE="{configured}"\n', encoding="utf-8")
            for cli_override in (False, True):
                with self.subTest(cli_override=cli_override):
                    args = ["--env-file", str(settings)]
                    if cli_override:
                        args += ["--soul-file", str(override)]
                    result, _, _, choose, _ = self.run_mock_game([action()], args, load_settings=True)
                    self.assertEqual(result, 0)
                    self.assertEqual(choose.call_args.kwargs["soul"],
                                     "Choose Mudkip." if cli_override else "Choose Torchic. 트레이너 NOVA")

    def test_explicit_missing_soul_file_fails_before_emulator_control(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.stderr"), self.assertRaises(SystemExit) as error:
                self.run_mock_game([action()], ["--soul-file", str(Path(directory) / "missing.md")])
            self.assertEqual(error.exception.code, 2)

    def test_missing_default_soul_is_optional(self):
        with patch("play_pokemon.Path.read_text", side_effect=FileNotFoundError):
            result, _, _, choose, _ = self.run_mock_game([action()])
        self.assertEqual(result, 0)
        self.assertEqual(choose.call_args.kwargs["soul"], "")

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

    def run_mock_game(self, decisions, extra_args=(), environment=None, load_settings=False, operator=None, steps=None, default_mode=False):
        operator = operator if operator is not None else Operator("Play")
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
            memory_directory = stack.enter_context(tempfile.TemporaryDirectory())
            stack.enter_context(patch.dict(os.environ, environment if environment is not None else {"OPENROUTER_API_KEY": "test-key", "MAIN_MODEL": "test-model"}, clear=True))
            mode_args = [] if default_mode or "--browser" in extra_args else ["--terminal"]
            stack.enter_context(patch("sys.argv", ["play_pokemon.py", *mode_args, "--memory-dir", memory_directory, "--steps", str(len(decisions) if steps is None else steps), *extra_args]))
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

    def test_browser_mode_starts_and_closes_dashboard_and_restores_stdout(self):
        import sys

        original = sys.stdout
        with patch("pokemon_web.Dashboard") as dashboard:
            result, emulator, _, _, _ = self.run_mock_game([action()], ["--browser", "--web-port", "0"])
        self.assertEqual(result, 0)
        dashboard.assert_called_once()
        self.assertEqual(dashboard.call_args.args[2], 0)
        dashboard.return_value.start.assert_called_once_with()
        dashboard.return_value.close.assert_called_once_with()
        emulator.act.assert_called_once()
        self.assertIs(sys.stdout, original)

    def test_browser_is_default_and_terminal_can_be_selected(self):
        for args in ([], ["--terminal"]):
            with self.subTest(args=args), patch("pokemon_web.Dashboard") as dashboard:
                result, _, _, _, _ = self.run_mock_game([action()], args, default_mode=True)
                self.assertEqual(result, 0)
                if args:
                    dashboard.assert_not_called()
                else:
                    dashboard.return_value.start.assert_called_once_with()
                    dashboard.return_value.close.assert_called_once_with()

    def test_custom_env_file_supplies_defaults_and_respects_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom settings.env"
            path.write_text('OPENROUTER_API_KEY=file-key\nMAIN_MODEL=file/model\nSKYEMU_URL=http://localhost:9090\nSKYEMU_SPEED=2\n', encoding="utf-8")
            for overrides in (False, True):
                with self.subTest(overrides=overrides):
                    args = ["--env-file", str(path)]
                    environment = {}
                    if overrides:
                        environment = {"OPENROUTER_API_KEY": "shell-key", "MAIN_MODEL": "shell/model"}
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
                environment = {"OPENROUTER_API_KEY": "test-key", "MAIN_MODEL": "test-model"}
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
        self.assertEqual(retry[5]["working_memory"]["notes"], "No previous observations.")
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
                "MAIN_MODEL='file/model'\nOTHER_SETTING=ignored\n",
                encoding="utf-8-sig",
            )
            os.environ["MAIN_MODEL"] = "shell/model"
            load_env(path)
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "test=key")
            self.assertEqual(os.environ["MAIN_MODEL"], "shell/model")
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
        with patch("play_pokemon.time.sleep"), patch("play_pokemon.WindowsSpeedControl") as control:
            emulator.set_speed(2)
        key = control.return_value
        self.assertEqual(emulator.speed, 2)
        self.assertEqual([call.args[0] for call in key.send.call_args_list], [False, True])
        self.assertFalse(any(call.kwargs.get("Emulator \uf04e") == 1 for call in emulator.api.call_args_list))
        emulator.release()
        self.assertNotIn("Emulator \uf04e", emulator.api.call_args.kwargs)
        emulator.restore_speed()
        key.send.assert_called_with(False)
        emulator.api.assert_called_with("run")
        self.assertEqual(emulator.speed, 1)

    def test_normal_speed_does_not_require_window_control(self):
        emulator = SkyEmu("http://localhost:8080")
        emulator.api = Mock()
        with patch("play_pokemon.WindowsSpeedControl") as control:
            emulator.set_speed(1)
        control.assert_not_called()
        emulator.api.assert_called_with("run")

    def test_speed_key_uses_configured_binding_and_can_be_released_after_failure(self):
        emulator = SkyEmu("http://localhost:8080")
        emulator.api = Mock()
        with patch.dict(os.environ, {"SKYEMU_FAST_FORWARD_KEY": "G"}), patch("play_pokemon.WindowsSpeedControl") as control, patch("play_pokemon.time.sleep"):
            control.return_value.send.side_effect = [None, OSError("message rejected"), None]
            with self.assertRaisesRegex(OSError, "message rejected"):
                emulator.set_speed(2)
            control.assert_called_once_with("G")
            emulator.restore_speed()
            control.return_value.send.assert_called_with(False)

    def test_window_control_failure_is_reported_without_claiming_2x(self):
        emulator = SkyEmu("http://localhost:8080")
        emulator.api = Mock()
        with patch("play_pokemon.WindowsSpeedControl", side_effect=ValueError("No SkyEmu window")), self.assertRaisesRegex(ValueError, "No SkyEmu window"):
            emulator.set_speed(2)
        self.assertEqual(emulator.speed, 1)

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

    def test_all_four_taps_allow_animation_time_at_each_speed(self):
        for speed in (1, 2):
            with self.subTest(speed=speed):
                clock = [0.0]
                presses = []
                releases = []
                emulator = SkyEmu("http://unused")
                emulator.speed = speed

                def record(endpoint, **params):
                    if any(params.values()):
                        presses.append(clock[0])
                    else:
                        releases.append(clock[0])

                def advance(seconds):
                    clock[0] += seconds

                emulator.api = record
                with patch("play_pokemon.time.monotonic", side_effect=lambda: clock[0]), patch("play_pokemon.time.sleep", side_effect=advance):
                    execution = emulator.act(action(["Right"] * 4), Operator("Play"), 0)

                self.assertEqual(len(presses), 4)
                self.assertEqual(len(releases), 4)
                for index in range(1, 4):
                    # Each later tap must wait out the game's animation window.
                    self.assertGreaterEqual((presses[index] - releases[index - 1]) * speed + 1e-9, 0.30)
                self.assertEqual(execution["completed_taps"], ["Right"] * 4)
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

    def test_model_observer_receives_request_before_inference_and_raw_rejected_response(self):
        events = []
        session = Mock()
        raw = {"choices": [{"message": {"content": "I would press A."}}]}

        def respond(*args, **kwargs):
            self.assertEqual(events[0][0], "request")
            return Mock(json=Mock(return_value=raw))

        session.post.side_effect = respond
        with self.assertRaises(ModelDecisionError):
            choose_action(session, "test-secret", "test-model", b"png", ["Play"], "", [],
                          observer=lambda kind, payload: events.append((kind, payload)))
        self.assertEqual([kind for kind, _ in events], ["request", "response"])
        self.assertEqual(events[0][1], session.post.call_args.kwargs["json"])
        self.assertEqual(events[1][1], raw)
        self.assertNotIn("test-secret", json.dumps(events))

    def test_model_observer_reports_transport_errors_and_redacts_provider_echoed_key(self):
        for failure in (True, False):
            with self.subTest(transport_failure=failure):
                events = []
                session = Mock()
                if failure:
                    session.post.side_effect = requests.ReadTimeout("Failed test-secret")
                else:
                    response = requests.Response()
                    response.status_code = 502
                    response._content = b'{"error":{"message":"Failed test-secret"}}'
                    session.post.return_value = response
                with self.assertRaises((requests.RequestException, ValueError)):
                    choose_action(session, "test-secret", "test-model", b"png", [], "", [],
                                  observer=lambda kind, payload: events.append((kind, payload)))
                self.assertEqual(events[-1][0], "error" if failure else "response")
                self.assertNotIn("test-secret", json.dumps(events))
                self.assertIn("[REDACTED]", json.dumps(events))

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
