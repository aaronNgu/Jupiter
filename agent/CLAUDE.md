# CLAUDE.md — luminque-ops

## Project overview

`luminque-ops` is a background agent for Windows that records GUI interactions
for retrospective business process analysis. It is distributed as a **single
signed `.exe`** built with PyInstaller. The same binary runs in four modes,
selected by a CLI flag.

The agent is not a real-time monitoring tool. Capture is local; a separate
sender process ships data to the Luminque cloud ingest endpoint on a schedule.
Analysis is async and performed server-side.

## Quick commands

```bash
# Install (requires uv — https://docs.astral.sh/uv/)
uv sync --extra dev

# Run tests
uv run pytest -v

# Run a specific test file
uv run pytest tests/test_sender.py -v

# Build the .exe (Windows only — must be run on a Windows machine or CI runner)
uv run pyinstaller luminque.spec

# Run the entry point from source (any mode)
uv run luminque --onboard
uv run luminque --capture
uv run luminque --send
uv run luminque --watchdog
```

## Architecture — the four modes

All four modes are invoked via the same entry point (`luminque/main.py`).
Mode selection is by the first CLI argument. No argparse — intentionally simple
to avoid PyInstaller import issues.

```
luminque.exe                 (no args)  → onboarding  (double-click UX)
luminque.exe --onboard                  → onboarding
luminque.exe --capture                  → long-running capture process
luminque.exe --send                     → one-shot send cycle
luminque.exe --watchdog                 → one-shot watchdog check
```

### --capture (luminque/captureV2/)

Native screenshot capturer (no openadapt-capture). One process, one capture
thread plus storage-free pynput listeners. Runs continuously from user login
until logout or restart by the watchdog. Writes to:

```
%APPDATA%\Luminque\recordings\recording.db
```

Key design decisions (see `luminque-capture-p3.md`):
- **Activity-gated sampling with change dedupe**: ~1 fps while the user is
  active (any input within 5 s), nothing while idle; a frame is kept only when
  a 64-px dhash says the screen changed since the last kept frame.
- Screenshots are downscaled to ≤1280 px wide and stored as PNG blobs in the
  same SQLite schema openadapt-capture used (`captureV2/schema.py`, stdlib
  sqlite3, WAL mode) — the sender reads either capturer's DB unchanged.
- Foreground window title/bounds are stamped per kept frame via raw user32
  calls (never pywinauto/UIA — the UIA tree walk was the old 50%-CPU bug).
- pynput hook failure degrades to continuous sampling; dedupe still bounds
  the kept-frame rate.
- **Capture-side disk guard**: every 5 min the capture thread nulls `png_data`
  older than 8h (backstopping the sender's 6h cap) and evicts oldest-first
  until under a 2 GiB size cap, then
  incrementally vacuums. Runs independently of the sender — the sender returns
  early on credential failure and may not run at all (unenrolled, `--send`
  task missing, exe quarantined), so without this an unenrolled machine would
  grow blobs until C: fills. Only nulls `png_data` (sender's `png_data != None`
  filter skips it), so it never fights the upload cursor; may evict unsent
  blobs when the sender is failing — intended tradeoff (disk bound > at-least-
  once delivery).

The legacy openadapt-capture wrapper remains at `luminque/capture/` until
captureV2 passes Windows VM validation (p3 §10 step 6). It is no longer
routed from main.py and `openadapt-capture` is no longer a dependency, so it
cannot run from a fresh checkout — rollback requires reverting both main.py
and pyproject.toml.

### --send (luminque/sender/)

Reads the capture DB, (Phase 2: scrubs PII), and POSTs gzip-compressed JSON
to the Luminque cloud ingest endpoint. Short-lived — runs and exits.

Key design decisions:
- Persistent cursor in `%APPDATA%\Luminque\sender_state.json`
  (`last_sent_action_event_id`). Cursor only advances on HTTP 200.
- At-least-once delivery. Server must deduplicate on `(machine_id, action_event.id)`.
- Screenshots are base64-encoded inline (no multipart). ~33% size overhead,
  mitigated by gzip.
- API key and endpoint URL stored in Windows Credential Manager via `keyring`.
  Never in plaintext config files.
- 6-hour local retention cap: `screenshot.png_data` is nullified (not deleted)
  after 6h to bound local storage. The capture-side guard (see below)
  backstops at 8h so disk stays bounded even when the sender never runs.

### --watchdog (luminque/watchdog/)

Short-lived process, runs every 5 minutes via Task Scheduler. Checks capture
process liveness and RSS. Restarts capture if dead or > 500 MB RSS.
Also performs a daily midnight restart (00:00–00:05 window) to clear memory drift.

### --onboard (luminque/onboarding/)

Tkinter UI. Shown on bare double-click. Obtains explicit informed consent,
copies the .exe to `%LOCALAPPDATA%\Programs\Luminque\luminque.exe`, registers
three Windows Scheduled Tasks (capture / sender / watchdog) via `schtasks.exe`,
and starts capture immediately as a detached subprocess.

No admin/UAC elevation required at any point. All tasks run as the current user.

## Key design decisions

1. **Single .exe, four modes** — avoids installer complexity. One file the IT
   admin can push via Intune/SCCM. See `luminque-deployment-p1.md`.

2. **Windows Task Scheduler, not a Windows Service** — services require admin
   install. Task Scheduler tasks can be registered per-user without elevation.

3. **No argparse** — `sys.argv[1]` string comparison only. Avoids PyInstaller
   hidden-import issues with argparse internals.

4. **Activity-gated sampling with change dedupe** — capture ~1 fps only while
   the user is active and keep a frame only when the screen actually changed
   (dhash on a thumbnail). Replaces the openadapt action-gated trigger, which
   captured 300 ms *after* each event (post-action frames, often transient)
   and burned CPU in the event pipeline. See `luminque-capture-p3.md` §3.

5. **keyring for credentials** — never store the API key in a file, env var,
   or the registry. Windows Credential Manager is the right place. `verify=True`
   on all HTTPS calls is non-negotiable.

6. **At-least-once delivery with server-side deduplication** — the sender never
   advances the cursor on failure. Simpler than two-phase commit; the server
   handles duplicates.

## Design docs

Full technical design documents live at:

```
/Users/aaron_other/Documents/Luminque.nosync/design-docs/
  luminque-capture-p1.md        (Superseded by p3) Capture mode:
                                openadapt-capture integration, action-gated
                                screenshots, memory management, data schema.
  luminque-capture-p3.md        CaptureV2: native screenshot capturer —
                                activity-gated sampling, change dedupe,
                                SQLite schema compatibility, sender contract.
  luminque-sender-p1.md         Sender mode: payload schema, cursor tracking,
                                gzip transport, credentials, retention cap,
                                heartbeat, module structure.
  luminque-deployment-p1.md     Deployment: onboarding UI, Task Scheduler
                                registration, watchdog logic, PyInstaller spec,
                                GitHub Actions pipeline, code signing,
                                directory layout on user machines, uninstall.
  luminque-deployment-p2.md     (Future) Phase 2 deployment considerations.
  luminque-sender-p2.md         (Future) PII scrubbing, spaCy integration.
```

Read these before implementing any submodule. The design docs contain exact
function signatures, SQL queries, schtasks commands, and edge-case handling.

## Windows-specific notes

- **All runtime paths use `%APPDATA%` and `%LOCALAPPDATA%`** resolved via
  `os.environ["APPDATA"]` and `os.environ["LOCALAPPDATA"]`. Never hardcode
  `C:\Users\...` paths.
- **`subprocess.Popen` for detached processes** must use
  `creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP`
  to prevent the child from dying when the parent exits.
- **`schtasks.exe`** is used for task registration. `/F` overwrites existing
  tasks safely. `/RU ""` means "current user, no password prompt". `/RL LIMITED`
  prevents UAC elevation.
- **UIPI** (User Interface Privilege Isolation) will cause `get_active_window_data()`
  to fail silently when the user is in an elevated process (admin cmd.exe, etc.).
  Wrap all window data calls in `try/except Exception`.
- **PyInstaller `--onedir`** builds a `dist\luminque\` directory. The exe
  launches directly from that directory with no extraction step. The 7-Zip SFX
  installer (`luminque-installer.exe`) wraps this directory into a single file
  for distribution. Do not reference the spec file or source tree at runtime.
- **PIL.ImageGrab** captures the primary monitor only on some PIL versions.
  Test on multi-monitor setups before shipping.
- **The `.exe` must be code-signed** with an EV certificate before distribution.
  Without a signature, Windows SmartScreen blocks non-technical users completely.
  See `luminque-deployment-p1.md` §8 for signing instructions and CI configuration.

## Related repos

| Repo | Purpose |
|---|---|
| `luminiq-hq/openadapt-capture` | Forked upstream capture library. **No longer a runtime dependency** — replaced by `luminque/captureV2/` (see `luminque-capture-p3.md`). Kept for reference until the legacy `luminque/capture/` wrapper is deleted. |
| `luminiq-hq/openadapt-privacy` | (Phase 2) PII scrubbing pipeline integrated into the sender. |

## Phase 1 scope

Phase 1 ships: capture + sender (raw, no PII scrubbing) + watchdog + onboarding.

Explicitly out of scope for Phase 1:
- PII scrubbing / openadapt-privacy integration (Phase 2)
- spaCy NLP model bundling (Phase 2)
- System tray icon
- Auto-update mechanism
- Programmatic uninstaller (manual removal via schtasks documented in deployment doc)
- Browser event capture
- Audio capture
- Multi-monitor screenshot support
- Windows Event Log integration
