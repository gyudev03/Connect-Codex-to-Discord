from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BOT_SCRIPTS = ("bot.py", "gemini_bot.py")


def main() -> None:
    processes: list[tuple[str, subprocess.Popen[bytes]]] = []
    try:
        for script in BOT_SCRIPTS:
            processes.append((script, subprocess.Popen([sys.executable, script], cwd=ROOT)))

        print("Codex bot and Gemini review bot are running. Press Ctrl+C to stop both.")

        while processes:
            for script, process in list(processes):
                return_code = process.poll()
                if return_code is not None:
                    processes.remove((script, process))
                    print(f"{script} exited with {return_code}.")
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping bots...")
    finally:
        for _, process in processes:
            if process.poll() is None:
                process.terminate()
        for _, process in processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    main()
