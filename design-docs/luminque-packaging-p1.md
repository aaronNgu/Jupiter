# Luminque Packaging — Phase 1 Technical Design

**Status:** Draft  
**Date:** 2026-05-26  
**Scope:** PyInstaller `--onedir` build, 7-Zip SFX installer, `--stop` mode

---

## 1. Overview

Luminque is packaged as a single `luminque-installer.exe` for distribution.
Internally it uses a PyInstaller `--onedir` build wrapped in a 7-Zip
self-extracting archive (SFX).

**Why `--onedir` instead of `--onefile`:**  
PyInstaller `--onefile` extracts its entire payload to `%TEMP%\_MEIxxxxxx` on
every launch (10–30 seconds per invocation). The watchdog runs every 5 minutes
and the sender runs every 45 minutes — both are short-lived processes that
cannot absorb this cost on every tick. With `--onedir`, PyInstaller produces a
directory of pre-extracted files and the executable launches directly from it
in ~200ms with no extraction step.

**Why 7-Zip SFX:**  
`--onedir` produces a folder, not a single file. A 7-Zip SFX archive wraps the
folder into a single `luminque-installer.exe` for distribution, so the user
still receives and runs one file.

---

## 2. Install Flow

```
Distribution artifact:  luminque-installer.exe  (7-Zip SFX)

User double-clicks luminque-installer.exe
  → SFX extracts contents to %LOCALAPPDATA%\Programs\Luminque\
      luminque.exe
      python311.dll
      ... (all DLLs and .pyc files)
  → SFX runs: luminque.exe --onboard
      → Tkinter consent UI → server URL + enrollment token
      → Enrolls device with server, stores credentials in Windows Credential Manager
      → Registers 3 scheduled tasks (capture, sender, watchdog)
      → Creates "Stop Luminque" shortcut on desktop
      → Starts capture immediately as a detached subprocess
      → Shows "Setup complete" dialog and exits

Every subsequent Task Scheduler invocation (capture, sender, watchdog):
  → luminque.exe runs from %LOCALAPPDATA%\Programs\Luminque\
  → No extraction step — starts in ~200ms
```

The installer is a one-time run. Scheduled tasks always invoke `luminque.exe`
directly from the installed location. The installer can be deleted after setup.

---

## 3. `luminque.spec` — Complete File

Build with: `pyinstaller luminque.spec` (Windows runner only).  
Output: `dist\luminque\` directory.

```python
# luminque.spec
#
# Build with:
#   pyinstaller luminque.spec
#
# Must be run on a Windows machine (or Windows GitHub Actions runner).
# Windows-specific binaries (pynput._win32, psutil._pswindows, etc.) are only
# available on Windows and will be missing from Mac builds.
#
# Output: dist\luminque\ directory. The CI pipeline wraps this into
# luminque-installer.exe using a 7-Zip SFX archive.
#
# Debugging tips:
#   - Set debug=True and console=True to surface hidden import errors.
#   - Run luminque.exe from a cmd.exe window and read the traceback.
#   - Add missing modules to hiddenimports, then revert debug/console flags.

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------

datas = []
datas += collect_data_files("certifi")      # SSL certs used by requests
# datas += collect_data_files("alembic")   # uncomment if openadapt-capture uses Alembic

# ---------------------------------------------------------------------------
# Hidden imports
# ---------------------------------------------------------------------------
# PyInstaller cannot trace dynamic imports inside pynput, psutil, PIL, or
# SQLAlchemy. List them explicitly. Add entries as missing imports are
# discovered during test builds.

hidden_imports = [
    # SQLAlchemy — dialect selected at runtime
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.pysqlite",
    "sqlalchemy.orm",

    # pynput — Windows input backend, not statically traceable
    "pynput.keyboard._win32",
    "pynput.mouse._win32",

    # Pillow — C extension loaded via importlib
    "PIL._imaging",
    "PIL.Image",
    "PIL.ImageGrab",

    # psutil — Windows-specific C extension
    "psutil._pswindows",
    "psutil._psutil_windows",

    # keyring — Windows Credential Manager backend
    "keyring.backends.Windows",

    # tkinter — may need explicit listing on some Python builds
    "tkinter",
    "tkinter.messagebox",

    # openadapt-capture internals — add as discovered during test builds
    # "openadapt_capture.db.models",
    # "openadapt_capture.config",
    # "openadapt_capture.recorder",
]

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

a = Analysis(
    ["luminque/main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "spacy",        # not bundled in Phase 1
        "torch",
        "numpy",        # verify openadapt-capture does not require numpy
        "matplotlib",
        "IPython",
        "jupyter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---------------------------------------------------------------------------
# EXE + COLLECT  (--onedir)
# ---------------------------------------------------------------------------
# EXE contains only the bootloader and Python scripts.
# COLLECT assembles the full dist\luminque\ directory alongside it.

exe = EXE(
    pyz,
    a.scripts,
    [],                              # binaries/datas live in COLLECT, not the exe
    name="luminque",
    debug=False,                     # set True when debugging hidden imports
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,                   # no cmd.exe window for any mode
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,                # None = match build machine (x64)
    codesign_identity=None,          # signing is done on luminque-installer.exe post-build
    entitlements_file=None,
    # icon="assets/luminque.ico",    # uncomment once assets/luminque.ico is created
    # version="version_info.txt",    # uncomment once version_info.txt is created
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="luminque",                 # produces dist\luminque\
)
```

---

## 4. `--stop` Mode

`luminque.exe --stop` kills all running Luminque processes and deletes the
three scheduled tasks. During onboarding, a "Stop Luminque" desktop shortcut
is created pointing to this mode.

### `luminque/main.py` — add dispatch branch

```python
elif mode == "--stop":
    from luminque.stop import run
    run()
```

The full dispatch block in `main.py` becomes:

```python
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--onboard"

    if mode == "--onboard":
        from luminque.onboarding import run
    elif mode == "--capture":
        from luminque.capture import run
    elif mode == "--send":
        from luminque.sender import run
    elif mode == "--watchdog":
        from luminque.watchdog import run
    elif mode == "--stop":
        from luminque.stop import run
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        sys.exit(1)

    run()
```

### New file: `luminque/stop/__init__.py`

```python
import os
import subprocess
import psutil

TASK_NAMES = ["LumniqueCapture", "LumniqueSender", "LumniqueWatchdog"]
STOP_FLAGS = {"--capture", "--watchdog", "--send"}
CURRENT_PID = os.getpid()


def run():
    _kill_luminque_processes()
    _delete_scheduled_tasks()


def _kill_luminque_processes():
    """Kill capture, sender, and watchdog processes.

    Watchdog is terminated first so it cannot restart capture
    before capture itself is killed.
    """
    targets = []
    for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            if proc.pid == CURRENT_PID:
                continue
            exe = proc.info["exe"] or ""
            cmdline = proc.info["cmdline"] or []
            if "luminque" in exe.lower() and STOP_FLAGS.intersection(cmdline):
                targets.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Watchdog first (index -1 sorts before 0)
    targets.sort(key=lambda p: -1 if "--watchdog" in (p.info["cmdline"] or []) else 0)

    for proc in targets:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def _delete_scheduled_tasks():
    for name in TASK_NAMES:
        subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", name],
            capture_output=True,
        )
```

### Desktop shortcut — add to `luminque/onboarding/__init__.py`

Add `_create_stop_shortcut(exe_path)` and call it from `_on_connect()` after
`register_all_tasks(exe_path)` and before `_start_capture_now(exe_path)`.
Failure is non-fatal — log and continue.

```python
def _create_stop_shortcut(exe_path: str) -> None:
    desktop = os.path.join(os.environ["USERPROFILE"], "Desktop")
    shortcut = os.path.join(desktop, "Stop Luminque.lnk")
    ps = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$s = $ws.CreateShortcut("{shortcut}"); '
        f'$s.TargetPath = "{exe_path}"; '
        f'$s.Arguments = "--stop"; '
        f'$s.Description = "Stop all Luminque processes and scheduled tasks"; '
        f'$s.Save()'
    )
    try:
        subprocess.run(["powershell", "-Command", ps], capture_output=True, check=True)
    except Exception as e:
        logging.warning(f"Could not create Stop shortcut: {e}")
```

---

## 5. `openadapt-capture` Fork Dependency

`pyproject.toml` must reference the fork as a git URL so the GitHub Actions
Windows runner can resolve it. Replace the local path entry:

```toml
# pyproject.toml

[tool.uv.sources]
openadapt-capture = { git = "https://github.com/<org>/openadapt-capture", rev = "<commit-sha>" }
```

- Replace `<org>` with the GitHub org or user that owns the fork.
- Replace `<commit-sha>` with the full SHA of the commit containing the
  Luminque-specific changes: action-gated screenshots (`read_screen_events`
  rewrite) and the `SynchronizedQueue` maxsize fix.
- Update `rev` each time the fork is updated.

---

## 6. `CLAUDE.md` — Windows-specific note to update

Find the note about PyInstaller and replace it with:

```
- **PyInstaller `--onedir`** builds a `dist\luminque\` directory. The exe
  launches directly from that directory with no extraction step. The 7-Zip SFX
  installer (`luminque-installer.exe`) wraps this directory into a single file
  for distribution. Do not reference the spec file or source tree at runtime.
```

---

## 7. Implementation Locations

| File | What to do |
|---|---|
| `luminque.spec` | Replace entire file with the spec in §3. |
| `.github/workflows/build.yml` | Replace entire file with the workflow in §4. |
| `luminque/main.py` | Add `--stop` dispatch branch per §5. |
| `luminque/stop/__init__.py` | Create new file with `run()`, `_kill_luminque_processes()`, `_delete_scheduled_tasks()` per §5. |
| `luminque/onboarding/__init__.py` | Add `_create_stop_shortcut()` and call it from `_on_connect()` per §5. |
| `pyproject.toml` | Change `openadapt-capture` source to git URL per §6. |
| `CLAUDE.md` | Update PyInstaller note per §7. |

---

## 8. Implementation Notes

**Re-install / update:** If Luminque is already installed and running, the SFX
will fail to overwrite DLLs held open by running processes. Run
`luminque.exe --stop` before running the installer again. Document this in the
user-facing FAQ.

**SFX env var expansion:** `ExtractPath="%LOCALAPPDATA%\Programs\Luminque"`
should expand correctly on modern Windows. Verify on a test machine before
shipping. If expansion does not work, omit `ExtractPath` from the SFX config
(the SFX will extract next to itself), and update `install_exe()` in
`luminque/onboarding/__init__.py` to use `shutil.copytree` to copy the entire
parent directory to `%LOCALAPPDATA%\Programs\Luminque\` before registering
tasks.

**Code signing:** Sign `luminque-installer.exe` as a whole — not individual
files inside the bundle. The `sign-windows` job in the workflow above already
does this.
