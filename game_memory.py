"""Durable gameplay notes; the planner supplies updates, never controller code."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re


ENTRY_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "evidence": {"type": "string"}},
    "required": ["text", "evidence"], "additionalProperties": False,
}
NAVIGATION_TASK_SCHEMA = {
    "type": ["object", "null"],
    "description": "Main model's navigation assignment for the submodel, or null when no delegation is appropriate.",
    "properties": {name: {"type": "string"} for name in
                   ("destination", "instructions", "arrival_condition", "constraints")},
    "required": ["destination", "instructions", "arrival_condition", "constraints"],
    "additionalProperties": False,
}


def validate_navigation_task(task):
    if task is None:
        return
    if (not isinstance(task, dict) or set(task) != set(NAVIGATION_TASK_SCHEMA["required"])
            or not all(isinstance(value, str) and value.strip() and len(value) <= 2000
                       for value in task.values())):
        raise ValueError("navigation_task needs nonempty destination, instructions, arrival_condition, and constraints (max 2000 characters each), or null")


MEMORY_UPDATE_SCHEMA = {
    "type": "object",
    "description": "Update persistent notes from the CURRENT observation, never predicted outcomes. Use empty arrays when nothing was learned.",
    "properties": {
        "current_location": {"type": "string", "description": "Observed location, or unknown if it cannot be identified."},
        "goal_hierarchy": {
            "type": "object",
            "description": "The active path: overall adventure goal -> milestone subgoal -> immediate task. Preserve it until evidence or operator instructions justify changing it. Record completed milestones in events.",
            "properties": {name: {"type": "string"} for name in ("main_goal", "subgoal", "task")},
            "required": ["main_goal", "subgoal", "task"], "additionalProperties": False,
        },
        **{name: {"type": "array", "maxItems": 10, "items": ENTRY_SCHEMA}
           for name in ("events", "observed_facts", "hypotheses")},
        "forget_knowledge": {"type": "array", "maxItems": 10, "items": {"type": "string"},
                             "description": "Exact text of obsolete or disproven facts/hypotheses to remove. New observations can promote or demote the same text between categories."},
    },
    "required": ["current_location", "goal_hierarchy", "events", "observed_facts", "hypotheses", "forget_knowledge"],
    "additionalProperties": False,
}


def validate_memory_update(update):
    def text(value):
        return isinstance(value, str) and bool(value.strip()) and len(value) <= 2000

    if not isinstance(update, dict) or set(update) != set(MEMORY_UPDATE_SCHEMA["required"]):
        raise ValueError("memory_update must include location, goal_hierarchy, events, observed_facts, hypotheses, and forget_knowledge")
    if not text(update["current_location"]):
        raise ValueError("memory_update.current_location must be nonempty text (or unknown)")
    goals = update["goal_hierarchy"]
    if not isinstance(goals, dict) or set(goals) != {"main_goal", "subgoal", "task"} or not all(text(v) for v in goals.values()):
        raise ValueError("memory_update.goal_hierarchy needs nonempty main_goal, subgoal, and task")
    for name in ("events", "observed_facts", "hypotheses", "forget_knowledge"):
        entries = update[name]
        if not isinstance(entries, list) or len(entries) > 10:
            raise ValueError(f"memory_update.{name} must be an array of at most 10 entries")
        for entry in entries:
            if name == "forget_knowledge":
                valid = text(entry)
            else:
                valid = (isinstance(entry, dict) and set(entry) == {"text", "evidence"}
                         and all(text(v) for v in entry.values()))
            if not valid:
                raise ValueError(f"memory_update.{name} entries need nonempty text and evidence (max 2000 characters)")
    facts = {entry["text"].strip().casefold() for entry in update["observed_facts"]}
    if any(entry["text"].strip().casefold() in facts for entry in update["hypotheses"]):
        raise ValueError("memory_update cannot classify the same knowledge as both observed and hypothetical")


def _write(path, value):
    """Replace whole files so an interrupted write leaves the previous version."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


class GameMemory:
    def __init__(self, directory, game, main_goal):
        self.directory = Path(directory)
        self.path = self.directory / "memory.json"
        self.data = {
            "version": 1,
            "working_memory": {
                "game": game, "current_goal": main_goal, "current_location": "unknown",
                "last_action": None, "last_result": None, "active_plan": [],
                "goal_hierarchy": {"main_goal": main_goal, "subgoal": "Assess current progress", "task": "Inspect the current screen"},
                "notes": "No previous observations.",
                "navigation_task": None,
            },
            "episodic_memory": [],
            "knowledge": {"observed_facts": [], "hypotheses": []},
        }
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                working = saved["working_memory"]
                validate_navigation_task(working.get("navigation_task"))
                working.setdefault("navigation_task", None)
                if saved["version"] != 1 or working["game"] != game:
                    raise ValueError("memory version or game does not match; select a separate --memory-dir")
                # Validate before allowing subsequent writes over existing notes.
                validate_memory_update({
                    "current_location": working["current_location"], "goal_hierarchy": working["goal_hierarchy"],
                    "events": [], "observed_facts": [], "hypotheses": [], "forget_knowledge": [],
                })
                if not isinstance(working["notes"], str) or not isinstance(working["active_plan"], list):
                    raise ValueError("invalid working memory")
                if (not isinstance(working["current_goal"], str)
                        or not all(isinstance(step, str) for step in working["active_plan"])
                        or (working["last_action"] is not None and not isinstance(working["last_action"], dict))
                        or (working["last_result"] is not None and not isinstance(working["last_result"], dict))):
                    raise ValueError("invalid working memory fields")
                for entries in (saved["episodic_memory"], saved["knowledge"]["observed_facts"], saved["knowledge"]["hypotheses"]):
                    if not isinstance(entries, list):
                        raise ValueError("invalid memory entries")
                    for entry in entries:
                        if not isinstance(entry, dict) or not all(isinstance(entry[k], str) for k in ("text", "evidence", "time")):
                            raise ValueError("invalid memory entry")
                self.data = saved
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Cannot load gameplay memory {self.path}: {exc}. Existing notes were preserved.") from exc

    def context(self):
        """Keep prompts bounded while retrieving relevant older notes as well."""
        working = self.data["working_memory"]
        terms = set(re.findall(r"\w{3,}", json.dumps(working).casefold()))

        def retrieve(entries, limit):
            recent = entries[-limit // 2:]
            older = entries[:-limit // 2]
            ranked = sorted(enumerate(older), key=lambda pair: (
                len(terms & set(re.findall(r"\w{3,}", pair[1]["text"].casefold()))), pair[0]), reverse=True)
            selected = {i for i, _ in ranked[:limit - len(recent)]}
            return [entry for i, entry in enumerate(older) if i in selected] + recent

        return deepcopy({
            "working_memory": working,
            "episodic_memory": retrieve(self.data["episodic_memory"], 40),
            "knowledge": {name: retrieve(entries, 60) for name, entries in self.data["knowledge"].items()},
            "resume_guidance": "Saved notes may precede a reset or loaded save. Reconcile them with the current screenshot. last_action may be unassessed; execution completion is not evidence of gameplay success.",
        })

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        _write(self.path, json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")
        # A readable text projection; JSON is authoritative if a write is interrupted.
        events = self.data["episodic_memory"]
        _write(self.directory / "events.txt", "".join(
            f"{event['time']} {event['text']} | Evidence: {event['evidence']}\n" for event in events))

    def accept(self, action):
        """Persist an accepted plan before executing; no predicted event is inferred."""
        update = action["memory_update"]
        validate_memory_update(update)
        if "navigation_task" in action:
            validate_navigation_task(action["navigation_task"])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        working = self.data["working_memory"]
        if "navigation_task" in action:
            working["navigation_task"] = deepcopy(action["navigation_task"])
        if working["last_action"] is not None:
            result = deepcopy(action["result"])
            if result["status"] == "no_previous_action":
                result = {"status": "uncertain", "details": "Resumed without the previous screenshot; saved action outcome is unverified."}
            working["last_result"] = {"action": deepcopy(working["last_action"]), **result}
        working.update({
            "current_goal": action["objective"], "current_location": update["current_location"],
            "active_plan": list(action["plan"]), "goal_hierarchy": deepcopy(update["goal_hierarchy"]),
            "notes": action["memory"][:6000],
            "last_action": {"buttons": list(action["buttons"]), "expected_result": action["expected_result"],
                            "execution": "pending; may not have been attempted", "time": now},
        })
        events = self.data["episodic_memory"]
        known_events = {entry["text"].strip().casefold() for entry in events}
        for entry in update["events"]:
            identity = entry["text"].strip().casefold()
            if identity not in known_events:
                events.append(deepcopy(entry) | {"time": now})
                known_events.add(identity)
        knowledge = self.data["knowledge"]
        remove = {entry.strip().casefold() for entry in update["forget_knowledge"]}
        for name in ("observed_facts", "hypotheses"):
            for entry in update[name]:
                identity = entry["text"].strip().casefold()
                # Reclassification requires evidence in the same accepted update.
                for category in knowledge:
                    knowledge[category] = [old for old in knowledge[category] if old["text"].strip().casefold() != identity]
                knowledge[name].append(deepcopy(entry) | {"time": now})
        for category in knowledge:
            knowledge[category] = [entry for entry in knowledge[category] if entry["text"].strip().casefold() not in remove]
        self.save()

    def record_execution(self, execution):
        self.data["working_memory"]["last_action"]["execution"] = deepcopy(execution)
        self.save()
