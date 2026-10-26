"""Ask for harness settings and save them to .env beside this script."""

from getpass import getpass
from pathlib import Path


def ask(label, default="", *, required=False, choices=()):
    while True:
        suffix = f" [{default}]" if default else ""
        value = input(f"{label}{suffix}: ").strip() or default
        if required and not value:
            print("Please enter a value.")
        elif choices and value not in choices:
            print(f"Choose one of: {', '.join(choices)}.")
        else:
            return value


def ask_path(label, default=""):
    value = ask(label, default)
    # Accept paths pasted using Windows' Copy as path command.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value


def main():
    path = Path(__file__).resolve().with_name(".env")
    exists = path.exists()
    try:
        if exists and input(f"{path} already exists. Replace it? [y/N]: ").strip().lower() not in ("y", "yes"):
            print("Cancelled. Existing settings were left unchanged.")
            return

        print("Enter your settings. Press Enter to accept a default in brackets.")
        key = ""
        while not key:
            key = getpass("OpenRouter API key (hidden): ").strip()
            if not key:
                print("Please enter your OpenRouter API key.")

        settings = {
            "OPENROUTER_API_KEY": key,
            "OPENROUTER_MODEL": ask("OpenRouter model", "google/gemini-2.5-flash"),
            "OPENROUTER_DECISION_MODEL": ask("Decision model (OpenRouter provider/model ID, optional; blank disables dialogue routing)"),
        }
        print("For automatic launch, enter a ROM path. The default executable is beside play_pokemon.py.")
        settings["SKYEMU_EXE"] = ask_path("SkyEmu executable path", "SkyEmu-v5-Windows.exe")
        settings["GAME_ROM"] = ask_path("Pokemon ROM path (optional)")
        settings["SKYEMU_SPEED"] = ask("Game speed (1 = normal, 2 = fast forward)", "2", choices=("1", "2"))
        settings["SKYEMU_URL"] = ask("SkyEmu server URL", "http://127.0.0.1:8080")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. No settings were written.")
        return

    # The harness strips enclosing quotes and preserves Windows backslashes.
    content = "# Pokemon AI harness settings\n" + "".join(
        f'{name}="{value}"\n' for name, value in settings.items()
    )
    with path.open("w" if exists else "x", encoding="utf-8") as env_file:
        env_file.write(content)
    print(f"Saved settings to {path}")


if __name__ == "__main__":
    main()
