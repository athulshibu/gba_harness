# Pokemon AI harness

`tests/test_skyemu.py` is your original connection test. `play_pokemon.py` adds a
vision model that chooses a sequence of one to four controller taps per decision.
It uses OpenRouter with the existing `requests` installation; no additional SDK is needed.

## Run in PowerShell

First, run the setup launcher from the project folder:

```powershell
cmd /k .\setup_codespace.bat
```

It creates `codespace` if needed, activates it, and installs `requests` and
`pillow`. The command leaves a Command Prompt open with the environment active.
You can also run `setup_codespace.bat` from an existing Command Prompt.

1. Activate the environment 
   ```powershell
   .\codespace\Scripts\activate.bat
   ```
2. In SkyEmu, enable HTTP Control Server in the advanced settings on port 8080
   once. The normal game window uses this saved setting when launched.
3. For OpenRouter models, create an API key at [OpenRouter](https://openrouter.ai/settings/keys).
   For Codex models, use the local CLI setup described under **How it works**.
   Create `.env` beside `play_pokemon.py` with the setup helper:

   ```powershell
   .\codespace\Scripts\python.exe .\create_env.py
   ```

   The helper asks for your API key (hidden while typing), main model, optional decision model and navigation submodel, executable
   and ROM paths, speed, and server URL, then saves `.env`. Press Enter to use
   the displayed defaults or leave optional paths blank. It asks before
   replacing an existing `.env`. The resulting file looks like:

   ```dotenv
   OPENROUTER_API_KEY=your-openrouter-api-key
   MAIN_MODEL=google/gemini-2.5-flash
   DECISION_MODEL=
   SUBMODEL=
   SKYEMU_EXE=SkyEmu-v5-Windows.exe
   GAME_ROM="C:\Games\Pokemon_Emerald.gba"
   SKYEMU_SPEED=2
   SKYEMU_URL=http://127.0.0.1:8080
   ```

   Model selection uses `MAIN_MODEL`, `SUBMODEL`, and `DECISION_MODEL`.
   These replace the previous OpenRouter-prefixed model settings; update any
   custom settings files or shell variables. `OPENROUTER_API_KEY` remains the
   credential for models using the OpenRouter backend.

   The executable defaults to `SkyEmu-v5-Windows.exe` beside `play_pokemon.py`,
   including when `SKYEMU_EXE` is missing or blank. Set another executable
   path to override it. Use your actual ROM path during setup, then run
   (SkyEmu can be closed):

   ```powershell
   .\codespace\Scripts\python.exe .\play_pokemon.py --goal "Choose the fire starter and progress through the story." --steps 20
   ```

   The harness loads `.env` beside `play_pokemon.py` by default. To select a
   different settings file, pass `--env-file`:

   ```powershell
   .\codespace\Scripts\python.exe .\play_pokemon.py --env-file .\my.env --steps 0
   ```

   Relative settings paths use the current terminal directory; absolute paths
   also work. Only the selected file is loaded. An explicitly selected file
   must exist; the default `.env` may be absent when using shell variables.
   No new dependencies are needed.
   `.env` is excluded from Git. Existing shell environment variables take
   precedence over the selected file; `--model`, `--base`, and `--speed` take
   precedence over both. If you set
   these variables earlier in PowerShell, open a fresh terminal to use `.env`.
   The file supports blank lines, full-line comments, and optional quotes
   around values. Keep each setting on its own line.

   The harness launches the visible SkyEmu window when its control server is
   offline, waits up to 20 seconds for it, and loads `GAME_ROM`. Paths can
   contain spaces and Windows backslashes. Relative paths are resolved beside
   `play_pokemon.py`. If the server is already running with that same ROM,
   the harness continues the current game without reloading it. A different
   configured ROM is loaded from its normal boot sequence; this does not
   restore an emulator save state. SkyEmu's usual in-game save handling applies.
   Leave `GAME_ROM` empty to keep opening SkyEmu and your ROM manually.

   `SKYEMU_SPEED=2` sends the held 2x fast-forward hotkey directly to the SkyEmu
   game window on Windows. It uses the default `F` binding. If you changed that
   binding in SkyEmu, add `SKYEMU_FAST_FORWARD_KEY=G` (or your chosen letter or
   digit) to your settings file. It does not type into the terminal or move
   keyboard focus. Exactly one SkyEmu window must be open. Look for `2X` in
   SkyEmu's toolbar to verify it applied. Use `1`
   for normal speed, or pass `--speed 1` / `--speed 2` to override the file.
   Each gameplay tap is fixed at 50 ms at 1x (25 ms at 2x), followed
   by release. Sequences allow 300 ms of released game time (150 ms at
   2x) between taps so movement and menu animations can settle before the
   next input. Longer transitions can still require `WAIT` or a new observation.
   The model cannot select a longer hold. `WAIT` advances 250 ms
   of game time without a button press. Speed is restored to 1x when the harness exits
   normally or handles an error/Ctrl+C; the window is left open. `/pause`
   pauses AI actions only, so the game keeps running at the selected speed.
   Speed affects emulation, not model response latency. Hardcore mode may
   restrict fast forward; disable it for harness play. If your build does not
   support the window hotkey, use `SKYEMU_SPEED=1` and its toolbar controls.
   SkyEmu v5 merges HTTP inputs after processing window speed hotkeys, so
   sending its fast-forward icon through `/input` does not enable speedup.
   Startup prints the selected settings file and requested speed to help
   diagnose shell variables or `--env-file` / `--speed` overrides.

   Choose a model supporting both image inputs and tool calling.
   Change `MAIN_MODEL` or pass
   `--model provider/model` to test a different model. Text-only models cannot
   play from screenshots. Each decision sends a screenshot through OpenRouter
   to the selected provider and incurs API usage according to its pricing.
   Models prefixed with `codex/` instead use your locally authenticated Codex CLI.
   The default limit is 20 decisions, including
   decisions discarded after an instruction change. Use `--steps 0` to run
   continuously. Missing credentials or a model ID cause an error before the
   emulator is controlled.

   Add `--activity` to record each model query in `activity.json` in the current
   directory, or use `--activity .\logs\usage.json` to choose a file:

   ```powershell
   .\codespace\Scripts\python.exe .\play_pokemon.py --env-file .\my.env --activity --steps 0
   ```

   The file is a JSON array, saved after every response and appended to across
   runs. Each entry contains a UTC `timestamp`, `model`, `input_tokens`, and
   `output_tokens`. The response's model ID is used when available; otherwise
   the requested model ID is recorded. Main, router, and navigation queries are
   recorded using the full configured name for Codex (for example,
   `codex/gpt-6.1-sol/high`), including its reasoning effort. Queries are
   recorded separately, including replies rejected by local validation and
   queries discarded after an instruction change. Token counts come from the
   API's usage data; missing counts (including failed requests) are `null`, not
   estimated. Prompts, screenshots, replies, and API keys are not saved in this
   log. Logging works in both browser and terminal modes and is off by default.
   The default `activity.json` is excluded from Git.

4. Send messages through the browser while it plays. For terminal input instead,
   launch with `--terminal`, then type messages into the same terminal and press Enter:

   ```text
   Catch the next Pikachu you encounter.
   Avoid spending money until I say otherwise.
   /pause
   /resume
   /quit
   ```

   Instructions persist for the session. Newer instructions take priority.
   Live messages are marked as unanswered until an accepted decision replies
   in its `operator_response` field, shown as `Player:` in Live chat, or in
   optional `commentary`, shown as `Thoughts:`. Empty replies in both fields
   to unanswered messages are rejected before controller input. The
   model is instructed to answer questions before unrelated story progress,
   or explain missing information and prioritize checking it. Replies and
   unresolved questions are carried through action history and game memory.
   Commentary is intended for occasional changes in broader plans, strategy,
   feelings about the adventure, or operator answers, rather than button
   descriptions. It may be omitted or empty and is only printed when nonempty.
   The model is instructed to announce its initial plan, new goals, and
   meaningful changes in strategy in the same decision, even without an
   operator message. Continuing an already announced plan can stay quiet.
   There is no speaking interval or turn quota; recognizing meaningful plan
   changes and explaining them is guided by the prompt.
   A background thread receives them while the model is thinking. If they
   change before an action starts, that action is discarded and the next
   decision uses the new instructions. A message during a button press cuts
   that press short and cancels remaining taps in the sequence. Instruction following is model behavior and is not
   guaranteed; enforce essential restrictions in code if needed.

## Persistent AI instructions (soul file)

Edit `SOUL.md` beside `play_pokemon.py` to set the player's personality,
trainer name, preferred starter, team preferences, or challenge rules.
Its full text is loaded at startup and included in every model request in
both terminal and browser mode. Restart the player after editing the file.
For example, add `Choose Torchic as your starter` or `Use the trainer name NOVA`.

To choose another UTF-8 text or Markdown file:

```powershell
.\codespace\Scripts\python.exe .\play_pokemon.py --soul-file .\my-trainer.md --browser --steps 0
```

You can also set `SOUL_FILE=./my-trainer.md` in `.env`. Shell variables take
precedence over `.env`, and `--soul-file` overrides both. Relative custom paths
use the terminal's current directory. The default `SOUL.md` is optional;
an explicitly configured file must exist and be readable. An empty file adds
no preferences. Keep secrets out of this file: its content is sent to the model
provider and appears in the browser's model request view.

Session goals and live instructions override conflicting soul preferences.
The harness still validates controller actions; preferences guide the model
and are not code-enforced gameplay restrictions. The soul file stores your
instructions; automatic progress notes are saved separately as gameplay memory.

## Persistent gameplay memory and planning

Gameplay notes now survive restarts. The default `game_memory/` directory beside
`play_pokemon.py` contains:

- `memory.json`: working state, the active goal hierarchy, all recorded events,
  and long-term knowledge. Working state includes the game, current goal and
  location, last attempted action, last assessed result, short plan, and notes
  about choices, learned controls, and unresolved operator questions.
- `events.txt`: a readable, timestamped milestone log, such as choosing a starter
  or reaching a town. Repeated event text is deduplicated. This file is generated
  from `memory.json`; edit the JSON to correct saved notes.

The vision model manages an active **main goal -> subgoal -> task** hierarchy,
for example **Complete Pokemon Emerald -> Reach Rustboro City -> Travel through
Petalburg Woods**. Its short plan leads to the next validated button sequence.
The executor handles taps, releases, and interruption; it does not choose goals.
Completed milestones go into episodic memory so the hierarchy can advance.

Knowledge separates `observed_facts` from `hypotheses`. Each entry includes the
model's supporting observation or rationale. The model can revise or remove
disproven knowledge and promote hypotheses after observing evidence. These notes
depend on the model reading screenshots accurately; they are not independent
game-state verification.

Every request receives working memory, up to 40 events, and up to 60 entries per
knowledge category. Each selection combines recent entries with older entries
matching the current location and goals. The full history remains on disk.
Only accepted decisions update notes; rejected replies and replies discarded
after an operator instruction change leave notes unchanged. Plans are saved
before execution, then updated with actual completed/interrupted taps. Expected
results are stored separately from observed outcomes.

To use another playthrough's memory:

```powershell
.\codespace\Scripts\python.exe .\play_pokemon.py --memory-dir .\emerald-save-2 --steps 0
```

You can also set `GAME_MEMORY_DIR=./emerald-save-2` in `.env`; `--memory-dir`
overrides it. Relative paths use the terminal's current directory. `--game`
overrides `GAME_NAME` (default: `Pokemon Emerald`). A directory saved for another
game or containing invalid JSON is rejected before emulator control, preserving
the existing file. Use a separate directory for each game/save. The default
`game_memory/` directory is excluded from Git; custom directories need their own
ignore rule if appropriate.

Memory does not restore an emulator save state. After a restart, the planner
reconciles saved progress with the current screen and newest operator instructions.
The previous process's last sequence has no paired screenshot, so its outcome
remains unverified. Start with a new memory directory after beginning a new game
or switching saves. Saved memory is sent to the model provider on each decision
and appears in the browser's model request view.

## Browser view

The left panel opens to **Conversation**, which shows operator messages and
the player's commentary and direct replies, without gameplay diagnostics.
**Live chat** contains the full log, and **Model input / output** shows model
exchanges. All three tabs support arrow-key navigation. The model
tab keeps the latest 20 requests in memory, showing the complete request JSON
(expand Input), screenshot previews, and raw responses, including rejected
decisions and request errors. Requests appear while inference is pending.
Authorization headers are excluded and the API key is redacted.

Browser mode is the default, with the live chat log on the left, the emulator
on the right, and an instruction box at the bottom:

```powershell
.\codespace\Scripts\python.exe .\play_pokemon.py --steps 0
```

Use `--terminal` for terminal input without the browser view. The existing
`--browser` option is also accepted.

The browser opens automatically at `http://127.0.0.1:8765`. If it does not,
open the printed URL yourself. Use `--web-port 8766` if that port is busy,
or `--web-port 0` to select a free port. Existing model, goal, speed, and
environment-file options work with browser mode. No new packages are needed.

Press Enter or **Send & resume** to automatically pause AI actions, add your
instruction, and resume. Shift+Enter adds a new line. The update is atomic:
an older model reply cannot act on the old instructions, and a running tap
is interrupted with the remaining taps skipped. **Pause**, **Resume**, and
**Stop** are also available separately. Sending an instruction resumes even
if you paused manually. The browser replaces terminal input in this mode;
logs still appear in both places. Closing the tab does not stop the player;
reopen the URL, press Stop, or use Ctrl+C in the terminal.

Game frames refresh at up to roughly 10 FPS through SkyEmu's `/screen`
endpoint. This is a local image stream without audio; SkyEmu still runs in
its desktop window. The game continues while AI actions are paused. The chat
log updates live as output arrives, with each model decision appearing when
its request completes; individual model tokens are not streamed. If you
scroll up, new output does not pull you back to the bottom.

The view is only served on your computer's loopback address. It stops when
the harness exits or reaches `--steps`; the last visible browser content
remains, with a disconnected status. Stop may wait for an outstanding model
request to return or time out. Each run starts a new session log.

## How it works

Use `codex/<model>/<reasoning_effort>` for `MAIN_MODEL` or `SUBMODEL` to run
the locally installed Codex CLI instead of OpenRouter. For example:

```dotenv
MAIN_MODEL=codex/gpt-6.1-sol/high
SUBMODEL=codex/gpt-6.1-sol/low
DECISION_MODEL=
```

This passes `--model gpt-6.1-sol` and `-c model_reasoning_effort="high"` for
the main model. Install Codex, make it available on PATH, and run `codex login`
before starting the harness. Codex uses its existing local authentication;
`OPENROUTER_API_KEY` can be blank when all configured models use Codex.
Mixing Codex and OpenRouter models is supported. `DECISION_MODEL` still uses
OpenRouter's Decisions API and requires its API key. Automatic navigation
delegation still requires a decision model and a saved navigation assignment.

Each Codex decision starts a fresh CLI process in a temporary directory with
read-only sandboxing, user configuration ignored, and shell tools disabled.
The harness supplies the same instructions, memory, history, and labeled
screenshots, then validates the returned JSON before controller input.
Temporary screenshots and output are removed after the call. Codex usage is
recorded in the existing activity log when supplied by CLI events. CLI failures
stop play; invalid action JSON follows the existing correction loop. Calls time
out after 180 seconds. Model access depends on your Codex account.

CLI options: [official OpenAI documentation](https://developers.openai.com/codex/cli/reference/).

Set `DECISION_MODEL=perplexity/pplx-decider-v1.2` in `.env` to send every
turn to a separate vision decision model first. Other models must support image
state and OpenRouter's Decisions API (`/api/alpha/decisions`).
The main model remains configured by `MAIN_MODEL`. The router returns:

- `WAIT`: wait without pressing a button while dialogue or animation finishes.
- `ADVANCE`: tap A once to advance a completed ordinary dialogue page.
- `SUBMODEL`: send the same input to the cheaper navigation submodel to follow
  the main planner's saved navigation assignment.
- `MODEL`: send the same screenshots, instructions, memory, and history to the
  main model for its normal gameplay decision.

Menus, battles, naming, choices, consequential confirmations, and uncertain
screens are routed to the main model by the router's instructions. Routine
navigation with an explicit assignment can go to `SUBMODEL`. The first
turn, unanswered operator messages, and correction attempts always invoke the
main model after routing. Malformed router replies also fall back to the main
model. Dialogue shortcuts retain the main model's goals and notes and record
actual execution without claiming observed success. The router still relies
on image interpretation, so it can misclassify screens.

Both models use the same OpenRouter API key and appear in the browser's model
request view. Each router call incurs its own API usage; `MODEL` turns also call
the main model. `--steps` counts turns, including shortcut turns, rather than
individual API calls. Use `--decision-model provider/model` to override the
environment, or `--decision-model ""` to disable routing. A blank or absent
`DECISION_MODEL` keeps the original main-model-only behavior.

Set `SUBMODEL=deepseek/deepseek-v4.1-flash-1017` to enable navigation
delegation. The submodel must support screenshot inputs and tool calling through
OpenRouter's Chat Completions API. `--submodel provider/model` overrides the file;
`--submodel ""` disables delegation. Automatic delegation also needs a configured
decision model. Missing submodel configuration or an assignment makes a
`SUBMODEL` recommendation fall back to the main model.

When a submodel is configured, every main-model action must include
`navigation_task`: an explicit object, or `null` to clear the assignment. It is
saved separately in working memory with these fields:

- `destination`: the specific location or landmark to reach.
- `instructions`: the route to follow, including known landmarks and uncertainty.
- `arrival_condition`: the visible evidence that means navigation is finished.
- `constraints`: restrictions and conditions requiring the submodel to stop.

The submodel receives this assignment, screenshots, history, goals, and notes.
It can execute directional taps and `WAIT`, while preserving the main planner's
objective, goal hierarchy, and navigation assignment. It reports
`navigation_status` as `continue`, `completed`, or `blocked`. Completion,
blockage, invalid replies, changes to the goal hierarchy, and proposed interaction
buttons hand control back to the main model on the same screenshot before any
submodel taps execute. Its raw response is included for the planner to reassess.
Accepted navigation actions update observed progress and execution history using
the existing memory and interruption handling. Dialogue shortcuts preserve the
assignment; the main model replaces or clears it. Older memory files load with
no assignment until the main model creates one.

Routine movement defaults to delegation: the main model must leave a navigation
assignment when starting a walking task, including routes needing local detours.
If it returns only directional taps with a null assignment, it must include
`navigation_hold_reason` explaining the need to retain control (for example,
directional selection inside a menu or a strategic navigation problem). Otherwise
the reply is rejected for correction. Local obstacle avoidance is submodel work;
repeated failed recovery or a change of destination goes to the main planner.
Live chat logs missing assignments, routing overrides, new assignments and the
main model's stated reason for retaining navigation, so a displayed `MODEL` can
be distinguished from an overridden `SUBMODEL` recommendation.

Live chat displays `SUBMODEL` in the same small route box as the other routes.
The model input/output tab shows the actual model ID for every request, including
the submodel and any subsequent main-model handoff. A turn can therefore involve
the router, submodel, and main model while counting as one `--steps` turn.

The harness captures `/screen`, sends the image plus instructions, concise
game notes, and the last 12 actions to OpenRouter's Chat Completions API, then validates a
`press_buttons` tool call before sending `/input`. Its `buttons` array contains
one to four ordered taps, such as `["Right", "Right", "Down", "A"]`.
The complete sequence is validated before any tap. Buttons are released in
`finally` blocks, including after errors. The model only gets controller
buttons, not arbitrary emulator endpoints or shell access.
Provider routing requires support for the supplied parameters. A provider
without compatible tool calling will fail instead of silently ignoring it.
The optional `parallel_tool_calls` setting is omitted for compatibility with
providers such as Gemma's Google AI Studio endpoint; exactly one tool call is
still required by local validation. HTTP failures show the model ID and
OpenRouter's error message to help diagnose routing or account settings.
The request uses `tool_choice: "auto"` for compatibility with Haiku providers
that reject forced named tools. The instructions request `press_buttons`, and
local validation accepts exactly one call to that tool. A plain-text reply is
rejected and requests a correction, just like any other invalid decision.
The harness validates buttons and decision fields locally; it does not require
provider-specific strict JSON schema support. No direct OpenAI API key is used.

Each decision includes a screen observation, current objective, short plan,
previous result, and next expected result. The next request includes labeled previous and current
screenshots plus the last action's expected result so the model can compare
outcomes. The player is instructed to choose its own trainer name and other
gameplay preferences and carry these choices forward in session memory. The
terminal displays its objective and plan alongside each sequence. A `Result of
[decision number]` line describes the previous sequence as `success`, `partial`,
`no_change`, or `uncertain`, based on the model's comparison of the screenshots.
The first decision says there is no previous sequence to assess. `Expect`
predicts the next sequence's outcome; it is separate from `Result`.
An `Execution` line reports how many taps finished. The history distinguishes
completed, incomplete, and skipped taps when interrupted. A completed tap does
not prove the game accepted it. The final sequence's visible outcome is assessed
on the next decision, so a run that ends at `--steps` does not make an additional
API call to assess it.
These are model-generated assessments, not independent verification that its
actions worked. Both screenshots are sent after the first action, increasing
image input usage. Restarting clears the screenshot/action window while restoring
the saved working state, milestones, and knowledge. Already
confirmed names are not changed automatically, and the game is never reset
by this improvement.

An incomplete or malformed model decision (for example, an empty
`expected_result`) is rejected without executing a button press. The harness
captures a fresh screenshot and requests a correction with validation feedback.
After three consecutive invalid decisions it pauses AI actions; send additional
instructions and `/resume` to try again, or `/quit` to stop. Each attempt counts
toward `--steps` and API usage, including rejected replies. HTTP, account, and
connection errors still stop the harness with their error message. No missing
button is guessed, and invalid decisions do not replace game memory.

The controller exposes sequences of individual taps rather than model-selected
holds. There is no model observation between taps within a sequence. The player
is instructed to use single taps when uncertain and short chains only for
navigation it understands, avoiding batches across unpredictable confirmations.
The player
is instructed to learn uncertain grids through reversible directional probes,
record observed transitions and unchanged moves, and change its approach when
repeating a direction produces no progress. These observations still depend
on the model reading the screenshots accurately.

**The game keeps running while the model thinks.** `/pause` pauses AI
actions, not the emulator. `/quit` prevents further actions, but an outstanding
API request must return or time out before the program exits; Ctrl+C interrupts
the main thread. The emulator stays running after exit, with inputs released.

Game notes are generated by the model and saved across runs. Screenshot
navigation can get stuck; this is a starter, not a guarantee of completing the
game. Useful next improvements are frame stepping for more precise control,
structured map exploration, and game-specific RAM observations.

For public broadcasting, capture the SkyEmu window or browser view in OBS.
The browser view does not configure stream hosting or connect stream chat.

References: [SkyEmu HTTP API](https://github.com/skylersaleh/SkyEmu/blob/dev/docs/HTTP_CONTROL_SERVER.md),
[OpenRouter image inputs](https://openrouter.ai/docs/guides/overview/multimodal/image-understanding),
[OpenRouter tool calling](https://openrouter.ai/docs/guides/features/tool-calling).

Run the harness tests from the project folder without calling a model or
controlling the emulator. This discovers `test_game_memory.py`,
`test_play_pokemon.py`, and `test_pokemon_web.py` in `tests/`, excluding the
manual SkyEmu connection test:

```powershell
.\codespace\Scripts\python.exe -m unittest discover -s .\tests -p "test_[gp]*.py" -v
```

## HTTPS connection troubleshooting

Test the public OpenRouter endpoint without starting SkyEmu, sending your API
key, or making a paid model call:

```powershell
.\codespace\Scripts\python.exe .\play_pokemon.py --check-connection
```

`CERTIFICATE_VERIFY_FAILED: EE certificate key too weak` happens during TLS,
before a model request is sent. The harness retries certificate verification
failures at most three times with verification still enabled. Other SSL errors
and request timeouts are not automatically retried because a model request
may already have been submitted. If verification keeps failing, check VPN,
proxy, or antivirus HTTPS inspection, or test on another trusted network.
The retry does not repair a consistently invalid or weak certificate. No TLS
security settings or certificate checks are disabled.


## Important Links

- [SkyEmu](https://sourceforge.net/projects/skyemu.mirror/)
- [VS Code](https://code.visualstudio.com/)
- [Tabby v1.0.238](https://github.com/Eugeny/tabby/releases/tag/v1.0.238)
- [Git for Windows](https://git-scm.com/install/windows)
