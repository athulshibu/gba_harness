import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from game_memory import GameMemory, validate_memory_update
from play_pokemon import ModelDecisionError, choose_action, parse_model_action
import test_play_pokemon as harness_tests
from test_play_pokemon import action, completion


def entry(text, evidence="Visible on the current screen"):
    return {"text": text, "evidence": evidence}


class MemoryTests(unittest.TestCase):
    def test_restart_restores_goals_choices_events_and_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = GameMemory(directory, "Pokemon Emerald", "Complete Pokemon Emerald")
            decision = action("Up")
            decision["memory"] = "Trainer NOVA chose Torchic. Need to check its moves."
            decision["memory_update"].update({
                "current_location": "Route 101",
                "goal_hierarchy": {"main_goal": "Complete Pokemon Emerald", "subgoal": "Reach Oldale Town", "task": "Follow the northern path"},
                "events": [entry("Selected Torchic")],
                "observed_facts": [entry("A confirms this menu", "Pressing A selected Torchic")],
                "hypotheses": [entry("The northern path leads to Oldale", "The route continues north")],
            })
            memory.accept(decision)
            execution = {"completed_taps": [], "incomplete_tap": "Up", "skipped_taps": [], "sequence_completed": False}
            memory.record_execution(execution)
            restored = GameMemory(directory, "Pokemon Emerald", "A new operator goal")
            self.assertEqual(restored.data, memory.data)
            context = restored.context()
            self.assertIn("Trainer NOVA", context["working_memory"]["notes"])
            self.assertEqual(context["working_memory"]["last_action"]["execution"], execution)
            self.assertIsNone(context["working_memory"]["last_result"])
            self.assertEqual(context["working_memory"]["goal_hierarchy"]["subgoal"], "Reach Oldale Town")
            self.assertIn("Selected Torchic", (Path(directory) / "events.txt").read_text(encoding="utf-8"))
            context["working_memory"]["notes"] = "changed"
            self.assertNotEqual(restored.data["working_memory"]["notes"], "changed")

    def test_events_deduplicate_and_knowledge_can_be_promoted_corrected_or_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = GameMemory(directory, "Pokemon Emerald", "Complete the game")
            decision = action()
            decision["memory_update"]["events"] = [entry("Entered Route 101")]
            decision["memory_update"]["hypotheses"] = [entry("The north exit leads to Oldale")]
            memory.accept(decision)
            memory.accept(decision)
            self.assertEqual(len(memory.data["episodic_memory"]), 1)
            decision["memory_update"]["hypotheses"] = []
            decision["memory_update"]["observed_facts"] = [entry("The north exit leads to Oldale", "Crossed it and read the town sign")]
            memory.accept(decision)
            self.assertEqual(memory.data["knowledge"]["hypotheses"], [])
            self.assertEqual(len(memory.data["knowledge"]["observed_facts"]), 1)
            decision["memory_update"]["observed_facts"] = []
            decision["memory_update"]["forget_knowledge"] = ["The north exit leads to Oldale"]
            memory.accept(decision)
            self.assertEqual(memory.data["knowledge"]["observed_facts"], [])

    def test_pending_actions_are_saved_and_result_is_associated_with_previous_action(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = GameMemory(directory, "Pokemon Emerald", "Complete the game")
            memory.accept(action("Up"))
            saved = json.loads(memory.path.read_text(encoding="utf-8"))
            self.assertIn("pending", saved["working_memory"]["last_action"]["execution"])
            memory.accept(action("Down"))
            working = memory.context()["working_memory"]
            self.assertEqual(working["last_action"]["buttons"], ["Down"])
            self.assertEqual(working["last_result"]["action"]["buttons"], ["Up"])
            self.assertEqual(working["last_result"]["status"], "uncertain")
            decision = action("Left")
            decision["result"] = {"status": "no_previous_action", "details": "New process"}
            GameMemory(directory, "Pokemon Emerald", "Complete the game").accept(decision)
            restored = GameMemory(directory, "Pokemon Emerald", "Complete the game")
            self.assertEqual(restored.data["working_memory"]["last_result"]["status"], "uncertain")
            self.assertIn("unverified", restored.data["working_memory"]["last_result"]["details"])

    def test_failed_atomic_replace_preserves_last_saved_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = GameMemory(directory, "Pokemon Emerald", "Complete the game")
            memory.accept(action())
            before = memory.path.read_bytes()
            with patch("game_memory.Path.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    memory.accept(action("Up"))
            self.assertEqual(memory.path.read_bytes(), before)

    def test_bad_memory_update_rejected_before_it_can_change_notes(self):
        for field, value in (("current_location", ""), ("events", ["Invented event"]),
                             ("observed_facts", [{"text": "A confirms"}]),
                             ("hypotheses", [entry("Guess")] * 11), ("goal_hierarchy", {"task": "Walk"})):
            with self.subTest(field=field):
                decision = action()
                decision["memory_update"][field] = value
                with self.assertRaises(ModelDecisionError):
                    parse_model_action(completion(decision))
        decision = action()
        decision.pop("memory_update")
        with self.assertRaisesRegex(ModelDecisionError, "memory_update"):
            parse_model_action(completion(decision))
        update = action()["memory_update"]
        update["observed_facts"] = [entry("A confirms")]
        update["hypotheses"] = [entry("A confirms")]
        with self.assertRaisesRegex(ValueError, "both observed and hypothetical"):
            validate_memory_update(update)

    def test_wrong_game_and_corrupt_notes_fail_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = GameMemory(directory, "Pokemon Emerald", "Complete the game")
            memory.save()
            before = memory.path.read_bytes()
            with self.assertRaisesRegex(ValueError, "does not match"):
                GameMemory(directory, "Pokemon Red", "Complete the game")
            self.assertEqual(memory.path.read_bytes(), before)
            memory.path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Existing notes were preserved"):
                GameMemory(directory, "Pokemon Emerald", "Complete the game")
            self.assertEqual(memory.path.read_text(encoding="utf-8"), "{broken")

    def test_context_retrieves_relevant_old_notes_and_bounds_prompt_without_deleting_history(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = GameMemory(directory, "Pokemon Emerald", "Complete the game")
            memory.data["working_memory"]["current_location"] = "Oldale"
            events = [entry("Oldale shortcut discovered") | {"time": "old"}]
            events += [entry(f"Milestone {i}") | {"time": "later"} for i in range(150)]
            memory.data["episodic_memory"] = events
            for name in memory.data["knowledge"]:
                memory.data["knowledge"][name] = list(events)
            context = memory.context()
            self.assertEqual(len(context["episodic_memory"]), 40)
            self.assertIn("Oldale shortcut discovered", [event["text"] for event in context["episodic_memory"]])
            self.assertEqual(len(context["knowledge"]["observed_facts"]), 60)
            self.assertEqual(len(memory.data["episodic_memory"]), 151)

    def test_structured_notes_are_sent_to_frontier_planner(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = GameMemory(directory, "Pokemon Emerald", "Complete the game")
            memory.accept(action())
            session = Mock()
            session.post.return_value.json.return_value = completion(action())
            choose_action(session, "key", "model", b"png", ["Play"], memory.context(), [])
            payload = session.post.call_args.kwargs["json"]
            context = json.loads(payload["messages"][1]["content"][0]["text"])
            self.assertEqual(context["game_memory"], memory.context())
            schema = payload["tools"][0]["function"]["parameters"]
            self.assertIn("memory_update", schema["required"])

    def test_loop_keeps_invalid_and_stale_decisions_out_of_persistent_memory(self):
        harness = harness_tests.HarnessTests()
        with tempfile.TemporaryDirectory() as directory:
            rejected = action()
            rejected["memory_update"]["events"] = [entry("Stale event")]
            from play_pokemon import Operator
            operator = Operator("Play")

            def decisions():
                yield ModelDecisionError("bad memory_update")
                operator.receive("Wait here")
                yield rejected
                yield action() | {"operator_response": "Waiting here."}

            result, emulator, _, _, _ = harness.run_mock_game(
                decisions(), ["--memory-dir", directory], operator=operator, steps=3)
            self.assertEqual(result, 0)
            emulator.act.assert_called_once()
            restored = GameMemory(directory, "Pokemon Emerald", "Complete the game")
            self.assertEqual(restored.data["episodic_memory"], [])
            self.assertEqual(restored.data["working_memory"]["last_action"]["execution"]["completed_taps"], ["A"])

    def test_loop_restores_memory_on_next_run(self):
        harness = harness_tests.HarnessTests()
        with tempfile.TemporaryDirectory() as directory:
            first = action()
            first["memory"] = "Trainer NOVA chose Torchic"
            first["memory_update"]["events"] = [entry("Selected Torchic")]
            harness.run_mock_game([first], ["--memory-dir", directory])
            result, _, _, choose, _ = harness.run_mock_game([action()], ["--memory-dir", directory])
            self.assertEqual(result, 0)
            restored_context = choose.call_args.args[5]
            self.assertEqual(restored_context["working_memory"]["notes"], first["memory"])
            self.assertEqual(restored_context["episodic_memory"][0]["text"], "Selected Torchic")
            self.assertIsNone(choose.call_args.args[7])  # No stale screenshot is reused.


if __name__ == "__main__":
    unittest.main()
