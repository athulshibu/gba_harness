
import subprocess

result = subprocess.run(
    [
        "codex.cmd",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "Say hello in one sentence."
    ],
    capture_output=True,
    text=True,
    timeout=120
)

print("Response:", result.stdout)
print("Errors:", result.stderr)
print("Return code:", result.returncode)
