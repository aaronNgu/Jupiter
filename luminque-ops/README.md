# luminque-ops

Background agent for Windows that captures GUI interactions for retrospective business process analysis. Distributed as a single signed `.exe` built with PyInstaller.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python package manager
- Python 3.11+
- For building the `.exe`: Windows machine or Windows CI runner

## Setup

```bash
# Clone
git clone https://github.com/luminiq-hq/luminque-ops
cd luminque-ops

# Install dependencies (including dev)
uv sync
```

## Running locally (development)

```bash
# Onboarding UI (default on bare double-click)
uv run luminque --onboard

# Start capture process
uv run luminque --capture

# Run one send cycle
uv run luminque --send

# Run one watchdog check
uv run luminque --watchdog
```

## Tests

```bash
# Run all tests
uv run pytest -v

# Run a specific module's tests
uv run pytest tests/test_sender.py -v
```

## Building the .exe

Must be run on a Windows machine or the GitHub Actions Windows runner:

```bash
uv run pyinstaller luminque.spec
# Output: dist/luminque.exe
```

The GitHub Actions `build.yml` workflow handles this automatically on push to `main` and version tags. The artifact is uploaded as `luminque-windows-exe`.

## Project structure

```
luminque/
  main.py          # entry point — routes --capture/--send/--watchdog/--onboard
  capture/         # wraps openadapt-capture (action-gated screenshots)
  sender/          # reads DB, gzips, POSTs to ingest endpoint
  watchdog/        # process health checks, restarts capture if needed
  onboarding/      # tkinter consent UI + Task Scheduler registration
tests/
.github/workflows/
  test.yml         # pytest on mac + windows
  build.yml        # PyInstaller .exe build + signing
luminque.spec      # PyInstaller build config
```

## On-machine layout (Windows)

```
%APPDATA%\Luminque\
  recordings\<timestamp>\recording.db   # capture output
  sender_state.json                     # send cursor
  machine_id                            # stable device UUID
  logs\                                 # process logs

%LOCALAPPDATA%\Programs\Luminque\
  luminque.exe                          # installed binary
```

## Related repos

| Repo | Purpose |
|---|---|
| `luminiq-hq/openadapt-capture` | Forked capture library — action-gated screenshot modification |
| `luminiq-hq/openadapt-privacy` | Phase 2 — PII scrubbing pipeline |

## Design docs

Full technical design documents are in `design-docs/` (see CLAUDE.md for details).
