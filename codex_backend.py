"""Run screenshot decisions through the locally authenticated Codex CLI."""

import base64
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def codex_settings(model):
    """Return (model, effort) for codex/model/effort, or None for other backends."""
    if not model or not model.startswith("codex/"):
        return None
    parts = model.split("/")
    if len(parts) != 3 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts[1:]):
        raise ValueError("Codex model must use codex/<model>/<reasoning_effort>, e.g. codex/gpt-6.1-sol/high")
    return parts[1], parts[2]


def strict_schema(schema):
    """Codex structured output requires every object property to be required."""
    schema = deepcopy(schema)
    if "properties" in schema:
        schema["required"] = list(schema["properties"])
        schema["additionalProperties"] = False
        schema["properties"] = {key: strict_schema(value) for key, value in schema["properties"].items()}
    if "items" in schema:
        schema["items"] = strict_schema(schema["items"])
    return schema


def request_codex(payload):
    model, effort = codex_settings(payload["model"])
    executable = shutil.which("codex") or shutil.which("codex.cmd")
    if not executable:
        raise ValueError("Codex CLI was not found on PATH. Install Codex and run codex login first.")
    with tempfile.TemporaryDirectory(prefix="pokemon-codex-") as directory:
        root = Path(directory)
        schema_path = root / "action.schema.json"
        answer_path = root / "action.json"
        schema_path.write_text(json.dumps(strict_schema(payload["tools"][0]["function"]["parameters"])), encoding="utf-8")
        command = [executable, "exec", "--ignore-user-config", "--ephemeral",
                   "--skip-git-repo-check", "--sandbox", "read-only",
                   "--model", model, "-c", f'model_reasoning_effort="{effort}"',
                   "-c", "features.shell_tool=false", "-c", "project_doc_max_bytes=0",
                   "--output-schema", str(schema_path), "--output-last-message", str(answer_path), "--json"]
        prompt = ["Choose a Pokemon controller action using only the supplied context and attached screenshots. "
                  "Return the press_buttons arguments as the final JSON object matching the output schema. "
                  "Do not execute tools or control the emulator. Use empty strings for optional text when unused."]
        image_number = 0
        for message in payload["messages"]:
            prompt.append("\n" + message["role"].upper() + ":")
            content = message["content"]
            if isinstance(content, str):
                prompt.append(content)
                continue
            for part in content:
                if part["type"] == "text":
                    prompt.append(part["text"])
                elif part["type"] == "image_url":
                    image_number += 1
                    image_path = root / f"screenshot-{image_number}.png"
                    image_path.write_bytes(base64.b64decode(part["image_url"]["url"].split(",", 1)[1], validate=True))
                    command.extend(["--image", str(image_path)])
                    prompt.append(f"[Attached image {image_number}: {image_path.name}]")
        command.append("-")
        try:
            result = subprocess.run(command, input="\n".join(prompt), cwd=root,
                                    capture_output=True, text=True, encoding="utf-8", timeout=180,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"Codex model {model} timed out after 180 seconds") from exc
        if result.returncode:
            raise ValueError(f"Codex model {model} exited with code {result.returncode}: {result.stderr.strip()}")
        usage = None
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "turn.completed":
                usage = event.get("usage")
        try:
            arguments = answer_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError(f"Codex model {model} returned no final action") from exc
        # Reuse the same parser, correction loop, and delegation checks as HTTP models.
        return {"model": payload["model"], "usage": usage, "choices": [{"finish_reason": "tool_calls", "message": {
            "tool_calls": [{"type": "function", "function": {"name": "press_buttons", "arguments": arguments}}]
        }}]}
