"""
luminque.main — entry point for the luminque.exe bundle.

Detects operating mode from the first CLI argument and routes to the
appropriate submodule. Bare double-click (no args) falls through to
onboarding, which is the expected UX for non-technical Windows users.

Usage:
    luminque.exe                  # double-click → onboarding
    luminque.exe --onboard        # explicit onboarding
    luminque.exe --capture        # long-running capture process
    luminque.exe --send           # one-shot send cycle
    luminque.exe --watchdog       # one-shot watchdog check
    luminque.exe --stop           # kill all Luminque processes and delete scheduled tasks
"""

import multiprocessing
import sys


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--onboard"

    if mode == "--onboard":
        from luminque.onboarding import run
    elif mode == "--capture":
        from luminque.captureV2 import run
    elif mode == "--send":
        from luminque.sender import run
    elif mode == "--watchdog":
        from luminque.watchdog import run
    elif mode == "--stop":
        from luminque.stop import run
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        print(
            "Valid modes: --onboard, --capture, --send, --watchdog, --stop",
            file=sys.stderr,
        )
        sys.exit(1)

    run()


if __name__ == "__main__":
    # MUST be the first call in the frozen exe. On Windows, multiprocessing
    # uses the "spawn" start method, which re-launches luminque.exe for each
    # child process (e.g. openadapt-capture's writer processes). Without this,
    # the spawned child re-runs main() instead of executing its target, so the
    # writer processes never start. See PyInstaller docs on multiprocessing.
    multiprocessing.freeze_support()
    main()
