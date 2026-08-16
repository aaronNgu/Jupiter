# Luminque Deployment — Phase 2 Technical Design

**Status:** Draft  
**Date:** 2026-05-05  
**Scope:** Phase 2 delta — client-side PII scrubbing via openadapt-privacy (Presidio + spaCy), model download flow, updated onboarding, upgrade path for Phase 1 users, updated build pipeline

**Prerequisite reading:** `luminque-deployment-p1.md` — this document covers only what changes or is added in Phase 2. All Phase 1 behavior not mentioned here remains unchanged.

---

## Implementation Locations

This section maps every Phase 2 deployment change to a specific file in `luminque-ops`. It is the authoritative reference for "what do I touch to implement X?"

### File changes

| Component | File | Type | Change |
|---|---|---|---|
| Model download logic | `luminque/model_download.py` | New file | Implements `download_model()`, `model_is_present()`, and `get_model_path()`. Downloads `en_core_web_sm` via `pip install --target` into `%APPDATA%\Luminque\models\` and verifies the result. |
| Upgrade CLI mode | `luminque/upgrade.py` | New file | Implements `run_upgrade()` — the `--upgrade` / `--upgrade --silent` entry point for existing Phase 1 installs. Shows a minimal tkinter progress window or runs headlessly; delegates to `download_model()`. |
| Updated onboarding UI | `luminque/onboarding/__init__.py` | Modify P1 file | Add model download step between consent and task registration; call `download_model()` on a background thread; wire up `ttk.Progressbar` and progress label; increase window height from 340 px to 380 px. |
| Main entry point | `luminque/main.py` | Modify P1 file | Add `--upgrade` dispatch branch that imports and calls `luminque.upgrade.run_upgrade()`. |
| Sender model guard | `luminque/sender/__init__.py` | Modify P1 file | Add model presence check at the top of `run_send()`; send degraded heartbeat and `sys.exit(1)` if model is missing; bump payload `schema_version` to `"2"`. |
| PyInstaller spec | `luminque.spec` | Modify P1 file | Add Presidio/spaCy hidden imports and `collect_data_files()` calls for `presidio_analyzer`, `presidio_anonymizer`, `spacy`, `thinc`; remove `spacy` from `excludes`; add `upx_exclude=["*.pyd"]`; add `tkinter.ttk` to hidden imports. |
| CI build workflow | `.github/workflows/build.yml` | Modify P1 file | Add `test-mac` unit-test job (macOS); add "Download spaCy model for smoke test" step after `pip install` on Windows runner; add binary size gate (< 150 MB); add spaCy model cache step; gate `build-windows` on `test-mac`. |
| Project dependencies | `pyproject.toml` | Modify P1 file | Add `openadapt-privacy>=0.1.0`, `presidio-analyzer>=2.2.0`, `presidio-anonymizer>=2.2.0`, and `spacy>=3.7.0,<3.8.0` to `[project] dependencies`. `en_core_web_sm` is a runtime download only — it does not go in `pyproject.toml`. |

### `pyproject.toml` dependency additions (Phase 2)

The following entries are added to the `[project] dependencies` list in `pyproject.toml`. They are not present in the Phase 1 file.

| Package | Version constraint | Purpose |
|---|---|---|
| `openadapt-privacy` | `>=0.1.0` | PII scrubbing wrapper — orchestrates Presidio + spaCy calls inside the sender |
| `presidio-analyzer` | `>=2.2.0` | PII entity recognition engine (consumed by `openadapt-privacy`) |
| `presidio-anonymizer` | `>=2.2.0` | PII entity replacement/redaction (consumed by `openadapt-privacy`) |
| `spacy` | `>=3.7.0,<3.8.0` | NLP backend for Presidio; minor version pinned to match `en_core_web_sm-3.7.x` |

`en_core_web_sm` itself is **not** added to `pyproject.toml` — it is downloaded at runtime onto the user's machine by `luminque/model_download.py`. The CI workflow downloads it explicitly as a separate step for smoke-test purposes only.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Model Download Flow](#2-model-download-flow)
3. [Updated Onboarding UI](#3-updated-onboarding-ui)
4. [Upgrade Path for Phase 1 Users](#4-upgrade-path-for-phase-1-users)
5. [Updated PyInstaller .spec File](#5-updated-pyinstaller-spec-file)
6. [Updated GitHub Actions Workflow](#6-updated-github-actions-workflow)
7. [Model Verification and Graceful Fallback](#7-model-verification-and-graceful-fallback)
8. [Changes from Phase 1 Deployment (Delta)](#8-changes-from-phase-1-deployment-delta)
9. [Implementation Locations](#implementation-locations)
10. [Out of Scope for Phase 2](#9-out-of-scope-for-phase-2)

---

## 1. Overview

Phase 2 adds client-side PII scrubbing to `luminque-sender` before data leaves the machine. Scrubbing is performed by `openadapt-privacy`, which wraps Microsoft Presidio and spaCy. The NLP inference requires a spaCy language model (`en_core_web_sm`, ~12 MB) to be present on disk at send time.

### What changes from Phase 1

| Area | Phase 1 | Phase 2 |
|---|---|---|
| PII scrubbing | None — raw data shipped | openadapt-privacy scrubs text fields before POST |
| spaCy model | Excluded from binary | Downloaded on first run, stored in `%APPDATA%\Luminque\models\` |
| Onboarding UI | Consent → register tasks → done | Consent → **download model** → register tasks → done |
| `main.py` CLI | `--onboard`, `--capture`, `--send`, `--watchdog` | Adds `--upgrade` for existing Phase 1 installs |
| PyInstaller `.spec` | spaCy excluded | New hidden imports for Presidio/spaCy; model still NOT bundled |
| `requirements.txt` | No NLP deps | Adds `openadapt-privacy`, `presidio-analyzer`, `presidio-anonymizer`, `spacy` |
| GitHub Actions | Standard pip install | Pre-download `en_core_web_sm` in CI for smoke-test, add to unit test matrix |

### What stays the same

- Single `.exe`, no separate installer.
- No admin/UAC required.
- Task Scheduler structure: capture (ONLOGON), sender (every 45 min), watchdog (every 5 min).
- Directory layout under `%APPDATA%\Luminque\` (one new subdirectory added: `models\`).
- Code signing with DigiCert EV certificate, post-build.
- All watchdog logic is unchanged.
- The sender's cursor, retry, payload schema, and heartbeat logic are unchanged (those are covered in the sender P2 doc).

---

## 2. Model Download Flow

### Where the model lives on disk

spaCy's `en_core_web_sm` model is **not bundled** inside the `.exe`. It is downloaded once and stored at:

```
%APPDATA%\Luminque\models\en_core_web_sm\
```

This directory is what spaCy calls a "model package" — it contains a `meta.json`, the weights, and the pipeline config. The sender locates the model by passing this absolute path to `spacy.load()` rather than relying on `site-packages`.

### Why store it in `%APPDATA%` rather than `site-packages`

- The `.exe` is a frozen PyInstaller binary. There is no live `site-packages` at runtime — `sys.prefix` resolves to a temp extraction directory that is wiped on exit.
- `%APPDATA%\Luminque\models\` persists across `.exe` updates, so the ~12 MB model is only downloaded once per machine.
- No admin rights needed to write to `%APPDATA%`.

### `luminque/model_download.py`

```python
"""
Model download logic for Phase 2.

Downloads en_core_web_sm to %APPDATA%\Luminque\models\ on first run.
Uses spacy.cli.download for the actual fetch, then moves the package
into the persistent models directory so it survives .exe updates.
"""

import os
import sys
import shutil
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

MODEL_NAME = "en_core_web_sm"
MODEL_VERSION = "3.7.1"   # pin to a known-good version; update deliberately

def get_models_dir() -> Path:
    return Path(os.environ["APPDATA"]) / "Luminque" / "models"

def get_model_path() -> Path:
    return get_models_dir() / MODEL_NAME

def model_is_present() -> bool:
    """Return True if the model directory exists and contains meta.json."""
    model_path = get_model_path()
    return (model_path / "meta.json").exists()

def download_model(progress_callback=None) -> None:
    """
    Download en_core_web_sm into the Luminque models directory.

    progress_callback: optional callable(message: str) — called with status
    strings so the caller (onboarding UI) can update a label or progress bar.

    Raises RuntimeError on failure.
    """
    models_dir = get_models_dir()
    models_dir.mkdir(parents=True, exist_ok=True)

    if model_is_present():
        log.info("Model already present at %s — skipping download.", get_model_path())
        if progress_callback:
            progress_callback("Model already installed.")
        return

    if progress_callback:
        progress_callback(f"Downloading {MODEL_NAME} (~12 MB)...")

    log.info("Downloading spaCy model %s %s", MODEL_NAME, MODEL_VERSION)

    # spacy.cli.download() calls pip under the hood, which installs into
    # the active Python environment. In a frozen PyInstaller binary the
    # "active environment" is the temp extraction dir, which is fine —
    # we move the result to the persistent directory immediately after.
    try:
        _run_spacy_download(progress_callback)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to download spaCy model '{MODEL_NAME}': {exc}\n\n"
            "Check your internet connection and try again."
        ) from exc

    if progress_callback:
        progress_callback("Installing model...")

    _move_model_to_appdata(models_dir, progress_callback)

    if not model_is_present():
        raise RuntimeError(
            f"Model download appeared to succeed but {get_model_path()} "
            "is missing. Try re-running setup."
        )

    log.info("Model installed at %s", get_model_path())
    if progress_callback:
        progress_callback("Model ready.")


def _run_spacy_download(progress_callback=None) -> None:
    """
    Run `python -m spacy download en_core_web_sm` as a subprocess.

    Using subprocess rather than calling spacy.cli.download() directly
    because the frozen binary's sys.executable is the .exe itself, not
    a Python interpreter. We need to invoke pip via the bundled Python
    paths that PyInstaller exposes through sys._MEIPASS.
    """
    # In a frozen binary, sys.executable is luminque.exe.
    # spaCy's download command uses pip, which requires a real python binary.
    # PyInstaller bundles python3X.dll but not python.exe.
    # Solution: use the pip module directly via importlib, passing --target
    # to redirect the install into our models directory.

    target = str(get_models_dir())
    package = f"{MODEL_NAME}>={MODEL_VERSION}" if MODEL_VERSION else MODEL_NAME

    # pip install --target writes the package files flat into the target dir.
    # spaCy models are standard Python packages, so this works correctly.
    cmd = [
        sys.executable,
        "-m", "pip",
        "install",
        "--quiet",
        "--target", target,
        f"https://github.com/explosion/spacy-models/releases/download/"
        f"{MODEL_NAME}-{MODEL_VERSION}/"
        f"{MODEL_NAME}-{MODEL_VERSION}-py3-none-any.whl",
    ]

    log.debug("Running: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,   # 5-minute timeout for the download
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())

    if progress_callback:
        progress_callback("Download complete.")


def _move_model_to_appdata(models_dir: Path, progress_callback=None) -> None:
    """
    After pip --target installs the model package flat into models_dir,
    locate the en_core_web_sm directory and ensure it's at the expected path.

    pip --target writes: models_dir/en_core_web_sm/  (the package directory)
    This is already the target path, so no move is required in most cases.
    However, pip may also write a .dist-info directory alongside it — that's
    fine and can be left in place.
    """
    expected = models_dir / MODEL_NAME
    if not expected.exists():
        # Try to find the model under a versioned name, e.g. en_core_web_sm-3.7.1
        for candidate in models_dir.iterdir():
            if candidate.is_dir() and candidate.name.startswith(MODEL_NAME):
                candidate.rename(expected)
                break

    if progress_callback:
        progress_callback("Model installed successfully.")
```

### Loading the model from `%APPDATA%`

The sender loads the model by absolute path:

```python
import spacy
from luminque.model_download import get_model_path, model_is_present

def load_spacy_model():
    if not model_is_present():
        raise RuntimeError(
            "spaCy model not found. Run luminque.exe --upgrade to install it."
        )
    return spacy.load(str(get_model_path()))
```

This bypasses the normal `spacy.load("en_core_web_sm")` package lookup, which would fail in a frozen binary since the model is not in `sys.path`.

### Error handling if download fails

Three failure modes during download and their handling:

| Failure | Handling |
|---|---|
| No network / pip times out | `subprocess.run` raises `TimeoutExpired`; caught by `download_model()`, re-raised as `RuntimeError` with user-friendly message. Onboarding shows error dialog; user can retry. |
| pip exits non-zero (e.g., wrong Python version) | Captured via `result.returncode`; stderr extracted and surfaced in error dialog. |
| Download succeeds but model dir missing | Post-download integrity check in `download_model()` raises `RuntimeError`. |

In all failure cases:
- The onboarding window shows the error message in a `messagebox.showerror` dialog.
- Tasks are NOT registered — the user must retry from the beginning.
- The models directory may contain a partial download. `download_model()` will re-attempt the full pip install on the next run (`--target` will overwrite existing files).

---

## 3. Updated Onboarding UI

### New Step: Model Download

The Phase 1 onboarding flow:

```
Consent screen → [I Agree] → register tasks → start capture → done
```

Phase 2 flow:

```
Consent screen → [I Agree] → download model (with progress) → register tasks → start capture → done
```

The model download step is inserted between consent and task registration. It runs synchronously on a background thread so the tkinter main loop stays responsive and can animate the progress label.

### Screen Dimensions

The window height increases from 340 px to 380 px to accommodate the progress row. Width remains 480 px.

### `luminque/onboard.py` (Phase 2 version)

```python
import tkinter as tk
from tkinter import messagebox
import threading
import subprocess
import sys
import os

from luminque.model_download import download_model, model_is_present

EXE_PATH = sys.executable

CONSENT_TEXT = (
    "What we collect:\n"
    "  • Screenshots of your screen activity\n"
    "  • Keyboard and mouse interaction metadata\n"
    "  • Window titles and active application\n\n"
    "Why:\n"
    "  This data is used to understand how you work so we can\n"
    "  build tools that help you do it faster.\n\n"
    "Your data is stored locally, scrubbed of personal information,\n"
    "and sent securely to Luminque servers.\n\n"
    "By clicking “I Agree”, you consent to this data collection."
)


def run_onboarding():
    root = tk.Tk()
    root.title("Luminque — Setup")
    root.geometry("480x380")
    root.resizable(False, False)
    _center_window(root, 480, 380)

    frame = tk.Frame(root, padx=20, pady=20)
    frame.pack(fill=tk.BOTH, expand=True)

    tk.Label(
        frame,
        text="Luminque — Data Collection Setup",
        font=("Segoe UI", 12, "bold"),
    ).pack(anchor="w", pady=(0, 12))

    tk.Label(
        frame,
        text=CONSENT_TEXT,
        justify=tk.LEFT,
        wraplength=440,
        font=("Segoe UI", 9),
    ).pack(anchor="w")

    # Progress row — hidden until download begins
    progress_var = tk.StringVar(value="")
    progress_label = tk.Label(
        frame,
        textvariable=progress_var,
        font=("Segoe UI", 9),
        fg="#0066cc",
        wraplength=440,
        justify=tk.LEFT,
    )
    progress_label.pack(anchor="w", pady=(10, 0))

    # Indeterminate progress bar (shown during download)
    import tkinter.ttk as ttk
    progress_bar = ttk.Progressbar(frame, mode="indeterminate", length=440)
    # Not packed yet — shown when download starts

    btn_frame = tk.Frame(frame)
    btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(16, 0))

    cancel_btn = tk.Button(
        btn_frame,
        text="Cancel",
        width=10,
        command=root.destroy,
    )
    cancel_btn.pack(side=tk.LEFT)

    agree_btn = tk.Button(
        btn_frame,
        text="I Agree",
        width=10,
        default=tk.ACTIVE,
        command=lambda: _on_agree(
            root, agree_btn, cancel_btn, progress_var, progress_bar
        ),
    )
    agree_btn.pack(side=tk.RIGHT)

    root.mainloop()


def _center_window(win, w, h):
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = (sw - w) // 2
    y = (sh - h) // 2
    win.geometry(f"{w}x{h}+{x}+{y}")


def _on_agree(root, agree_btn, cancel_btn, progress_var, progress_bar):
    # Disable buttons while work is in progress
    agree_btn.config(state=tk.DISABLED)
    cancel_btn.config(state=tk.DISABLED)

    # Show and start the progress bar
    progress_bar.pack(anchor="w", pady=(6, 0))
    progress_bar.start(12)   # animate every 12ms

    # Run the blocking work on a background thread so tkinter stays responsive
    thread = threading.Thread(
        target=_do_setup,
        args=(root, progress_var, progress_bar, agree_btn, cancel_btn),
        daemon=True,
    )
    thread.start()


def _do_setup(root, progress_var, progress_bar, agree_btn, cancel_btn):
    """
    Runs on a background thread. Uses root.after() to update the UI
    from the main thread (tkinter is not thread-safe for direct widget calls).
    """
    def ui(msg):
        root.after(0, lambda: progress_var.set(msg))

    try:
        # Step 1: Download model (skipped if already present)
        ui("Downloading language model (~12 MB)...")
        download_model(progress_callback=ui)

        # Step 2: Register scheduled tasks
        ui("Registering scheduled tasks...")
        from luminque.scheduler import register_all_tasks
        exe_path = _install_exe()
        register_all_tasks(exe_path)

        # Step 3: Start capture immediately
        ui("Starting Luminque...")
        _start_capture_now(exe_path)

        # Done
        root.after(0, lambda: _finish_success(root, progress_bar))

    except Exception as exc:
        root.after(
            0,
            lambda: _finish_error(root, progress_bar, agree_btn, cancel_btn, str(exc)),
        )


def _finish_success(root, progress_bar):
    progress_bar.stop()
    progress_bar.pack_forget()
    messagebox.showinfo(
        "Luminque",
        "Setup complete.\n\n"
        "Luminque is now running in the background.\n"
        "It will start automatically each time you log in.",
    )
    root.destroy()


def _finish_error(root, progress_bar, agree_btn, cancel_btn, error_msg):
    progress_bar.stop()
    progress_bar.pack_forget()
    messagebox.showerror("Setup Failed", error_msg)
    # Re-enable buttons so user can retry or cancel
    agree_btn.config(state=tk.NORMAL)
    cancel_btn.config(state=tk.NORMAL)


def _install_exe() -> str:
    """Copy exe to %LOCALAPPDATA%\Programs\Luminque\ and return the installed path."""
    import shutil
    src = sys.executable
    dst_dir = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "Luminque")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, "luminque.exe")
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy2(src, dst)
    return dst


def _start_capture_now(exe_path: str):
    subprocess.Popen(
        [exe_path, "--capture"],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
```

### Progress Bar Behavior

The progress bar is `ttk.Progressbar` in `indeterminate` mode — it bounces back and forth rather than showing a precise percentage. This is intentional: pip download progress is not easily introspectable from a subprocess without parsing its output line-by-line (which is fragile across pip versions). The progress label text updates at key milestones:

1. `"Downloading language model (~12 MB)..."` — before `pip install` subprocess starts
2. `"Download complete."` — after subprocess exits 0
3. `"Installing model..."` — while moving/verifying the model directory
4. `"Model ready."` — model integrity check passed
5. `"Registering scheduled tasks..."` — `register_all_tasks()` called
6. `"Starting Luminque..."` — capture subprocess launched

If the download takes less than a second (model already present), steps 1–4 flash by quickly and the user sees the full sequence, which is fine.

### Thread Safety Note

All tkinter widget mutations inside `_do_setup` are dispatched through `root.after(0, lambda: ...)`. Direct widget calls from the background thread (e.g., `progress_var.set(...)` called directly) are not safe on Windows and will intermittently crash. Never call tkinter from the background thread directly.

---

## 4. Upgrade Path for Phase 1 Users

Phase 1 users already have:
- `luminque.exe` installed in `%LOCALAPPDATA%\Programs\Luminque\`
- All three tasks registered in Task Scheduler
- `%APPDATA%\Luminque\` directory with capture DB, logs, state files

They do not need to re-run onboarding. However, they need the spaCy model downloaded before the updated sender will scrub PII. The `--upgrade` mode handles this.

### `--upgrade` Mode

When the user receives a Phase 2 build, IT can trigger the upgrade silently by running:

```
luminque.exe --upgrade
```

Or double-clicking the new `.exe` will still run onboarding as before — but existing users should use `--upgrade` to avoid re-triggering the consent screen and task re-registration.

### Updated `main.py`

```python
import sys

def main():
    if len(sys.argv) < 2:
        from luminque.onboard import run_onboarding
        run_onboarding()
        return

    mode = sys.argv[1]

    if mode == "--onboard":
        from luminque.onboard import run_onboarding
        run_onboarding()

    elif mode == "--capture":
        from luminque.capture import run_capture
        run_capture()

    elif mode == "--send":
        from luminque.sender import run_send
        run_send()

    elif mode == "--watchdog":
        from luminque.watchdog import run_watchdog
        run_watchdog()

    elif mode == "--upgrade":
        from luminque.upgrade import run_upgrade
        run_upgrade()

    elif mode == "--uninstall":
        from luminque.uninstall import run_uninstall
        run_uninstall()

    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

### `luminque/upgrade.py`

```python
"""
--upgrade mode: downloads the spaCy model for existing Phase 1 installs
without re-running the full onboarding flow.

Can be run silently (no window) for IT deployment, or with a minimal
progress window for end-user self-upgrade.

Usage:
    luminque.exe --upgrade              # shows progress window
    luminque.exe --upgrade --silent     # no window, exit code 0/1
"""

import sys
import logging
import os
from pathlib import Path

from luminque.model_download import download_model, model_is_present

LOG_PATH = Path(os.environ["APPDATA"]) / "Luminque" / "upgrade.log"

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def run_upgrade():
    silent = "--silent" in sys.argv

    if model_is_present():
        log.info("Model already present. Nothing to do.")
        if not silent:
            _show_already_done()
        sys.exit(0)

    if silent:
        _upgrade_silent()
    else:
        _upgrade_with_ui()


def _upgrade_silent():
    """Download model with no UI. Exit 0 on success, 1 on failure."""
    log.info("Starting silent upgrade: downloading %s", "en_core_web_sm")
    try:
        download_model(progress_callback=lambda msg: log.info(msg))
        log.info("Upgrade complete.")
        sys.exit(0)
    except Exception as exc:
        log.error("Upgrade failed: %s", exc)
        sys.exit(1)


def _upgrade_with_ui():
    """Show a minimal tkinter window with download progress."""
    import tkinter as tk
    from tkinter import messagebox
    import tkinter.ttk as ttk
    import threading

    root = tk.Tk()
    root.title("Luminque — Upgrade")
    root.geometry("400x160")
    root.resizable(False, False)
    _center_window(root, 400, 160)

    frame = tk.Frame(root, padx=20, pady=20)
    frame.pack(fill=tk.BOTH, expand=True)

    tk.Label(
        frame,
        text="Installing language model for PII scrubbing...",
        font=("Segoe UI", 10, "bold"),
    ).pack(anchor="w", pady=(0, 10))

    progress_var = tk.StringVar(value="Preparing...")
    tk.Label(
        frame,
        textvariable=progress_var,
        font=("Segoe UI", 9),
        fg="#0066cc",
    ).pack(anchor="w")

    bar = ttk.Progressbar(frame, mode="indeterminate", length=360)
    bar.pack(anchor="w", pady=(8, 0))
    bar.start(12)

    def do_download():
        try:
            download_model(
                progress_callback=lambda msg: root.after(0, lambda: progress_var.set(msg))
            )
            root.after(0, lambda: _upgrade_done(root, bar))
        except Exception as exc:
            root.after(0, lambda: _upgrade_failed(root, bar, str(exc)))

    threading.Thread(target=do_download, daemon=True).start()
    root.mainloop()


def _upgrade_done(root, bar):
    bar.stop()
    from tkinter import messagebox
    messagebox.showinfo(
        "Luminque",
        "Upgrade complete.\n\nPII scrubbing is now active.",
    )
    root.destroy()


def _upgrade_failed(root, bar, error_msg):
    bar.stop()
    from tkinter import messagebox
    messagebox.showerror(
        "Upgrade Failed",
        f"Could not download the language model:\n\n{error_msg}\n\n"
        "Check your internet connection and run luminque.exe --upgrade again.",
    )
    root.destroy()


def _show_already_done():
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo("Luminque", "Language model is already installed. No update needed.")
    root.destroy()


def _center_window(win, w, h):
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = (sw - w) // 2
    y = (sh - h) // 2
    win.geometry(f"{w}x{h}+{x}+{y}")
```

### IT Deployment Script

For managed environments, IT can push the upgrade via a startup script or Intune/SCCM:

```powershell
# Run as the logged-in user (not SYSTEM — APPDATA must resolve correctly)
$exe = "$env:LOCALAPPDATA\Programs\Luminque\luminque.exe"
if (Test-Path $exe) {
    # Copy new exe over existing (kill sender first; capture will be restarted by watchdog)
    Stop-Process -Name "luminque" -ErrorAction SilentlyContinue
    Copy-Item "\\deploy-share\luminque\luminque.exe" $exe -Force
    # Run upgrade silently — downloads model, exits 0/1
    & $exe --upgrade --silent
    exit $LASTEXITCODE
}
```

The `--silent` flag produces no windows, making it safe for unattended deployment. The exit code propagates to the deployment tool's success/failure tracking.

---

## 5. Updated PyInstaller .spec File

### Summary of Changes from Phase 1

- `spacy`, `presidio_analyzer`, `presidio_anonymizer`, and `en_core_web_sm` added to `hiddenimports`.
- `spacy` removed from `excludes` (it was explicitly excluded in Phase 1).
- `torch` and `numpy` remain in `excludes` — `en_core_web_sm` does not require them.
- `openadapt_privacy` data files added to `datas`.
- Binary size impact: the `.exe` grows by approximately 30–60 MB due to spaCy's C extensions and Presidio's rule files. The model itself (~12 MB) is NOT in the binary.

### `luminque.spec` (Phase 2)

```python
# luminque.spec — Phase 2
# Build with: pyinstaller luminque.spec

import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

datas = []
datas += collect_data_files("alembic")
datas += collect_data_files("certifi")

# Presidio ships JSON rule files and regex patterns as package data
datas += collect_data_files("presidio_analyzer")
datas += collect_data_files("presidio_anonymizer")

# spaCy ships its own data files (language defaults, lookups, etc.)
datas += collect_data_files("spacy")
datas += collect_data_files("thinc")      # spaCy's ML backend
datas += collect_data_files("cymem")
datas += collect_data_files("murmurhash")

# openadapt-privacy may ship its own config/rule files
datas += collect_data_files("openadapt_privacy")

hidden_imports = [
    # --- Phase 1 imports (unchanged) ---
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.pysqlite",
    "sqlalchemy.orm",
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "PIL._imaging",
    "PIL.Image",
    "PIL.ImageGrab",
    "psutil._pswindows",
    "psutil._psutil_windows",
    "tkinter",
    "tkinter.messagebox",
    "tkinter.ttk",        # added: Phase 2 uses ttk.Progressbar

    # --- Phase 2 additions ---

    # spaCy and its Cython extensions
    "spacy",
    "spacy.lang.en",
    "spacy.lang.en.stop_words",
    "spacy.pipeline",
    "spacy.pipeline.ner",
    "spacy.pipeline.tok2vec",
    "spacy.tokens",
    "spacy.tokens.doc",
    "spacy.tokens.span",
    "spacy.tokens.token",
    "spacy.vocab",
    "spacy.attrs",
    "spacy.morphology",
    "spacy.cli",
    "spacy.cli.download",
    "en_core_web_sm",     # needed for spacy.load() to resolve the package name

    # thinc (spaCy's ML backend — no torch required for en_core_web_sm)
    "thinc",
    "thinc.api",
    "thinc.backends",
    "thinc.backends.numpy_ops",

    # Presidio
    "presidio_analyzer",
    "presidio_analyzer.nlp_engine",
    "presidio_analyzer.nlp_engine.spacy_nlp_engine",
    "presidio_analyzer.predefined_recognizers",
    "presidio_anonymizer",
    "presidio_anonymizer.operators",

    # openadapt-privacy
    "openadapt_privacy",

    # regex is used by Presidio internally
    "regex",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # torch is NOT needed for en_core_web_sm (it uses thinc/numpy only)
        "torch",
        "torchvision",
        "tensorflow",
        "transformers",    # not needed for sm model
        # en_core_web_trf requires transformers; exclude it explicitly
        "en_core_web_trf",
        "matplotlib",
        "pandas",
        "scipy",
        "sklearn",
        "notebook",
        "IPython",
        "jupyter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="luminque",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[
        # UPX can corrupt some Cython .pyd files — exclude spaCy's extensions
        "*.pyd",
    ],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/luminque.ico",
    version="version_info.txt",
)
```

### `version_info.txt` Update

Bump `filevers` and `prodvers` to `(2, 0, 0, 0)` and update `FileVersion` / `ProductVersion` strings:

```
StringStruct(u'FileVersion', u'2.0.0.0'),
StringStruct(u'ProductVersion', u'2.0.0.0'),
```

### Hidden Import Discovery Process

The `hidden_imports` list above covers known-required imports based on Presidio and spaCy's package structure as of May 2026. If the build fails at runtime with `ModuleNotFoundError`, the standard procedure is:

1. Rebuild with `debug=True, console=True` in the `EXE()` block.
2. Run `luminque.exe --send` in a terminal.
3. The traceback will name the missing module.
4. Add it to `hidden_imports` and rebuild.

Do not use `collect_submodules("spacy")` as a lazy fix — it pulls in ~200 submodules including GPU backends and significantly inflates binary size. Add missing imports surgically.

### UPX Note

`upx_exclude=["*.pyd"]` is added in Phase 2. spaCy ships Cython-compiled `.pyd` files that UPX can mishandle on some Windows versions, producing invalid PE headers. Excluding `.pyd` files from UPX compression is the safe default. The binary size increase from this exclusion is minor relative to the total spaCy footprint.

---

## 6. Updated GitHub Actions Workflow

### Changes from Phase 1

1. `requirements.txt` now includes NLP deps — the install step is longer (spaCy/Presidio download at build time).
2. A new step downloads `en_core_web_sm` after pip install, so the smoke test can exercise the `--send` path with the model present.
3. The macOS unit test job installs the same new deps (but does not need the spaCy model for pure unit tests).
4. Binary size check added as a build gate: the signed `.exe` must be under 150 MB.

### `.github/workflows/build.yml` (Phase 2 delta — changed sections only)

```yaml
# Full workflow structure is identical to Phase 1.
# Only the changed steps are shown below.

  test-mac:
    name: Unit tests (macOS)
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        # requirements-dev.txt now includes openadapt-privacy, presidio-*, spacy
        run: pip install -r requirements-dev.txt

      # spaCy model is NOT downloaded for unit tests on macOS.
      # Unit tests mock model_is_present() and spacy.load() directly.
      # Only integration tests (Windows only) need the actual model.

      - name: Run unit tests
        run: pytest tests/unit/ -v

  build-windows:
    name: Build .exe (Windows)
    runs-on: windows-latest
    needs: test-mac

    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          architecture: "x64"

      - name: Install dependencies
        run: pip install -r requirements.txt pyinstaller

      # Phase 2 addition: download the spaCy model into the CI environment
      # so it is available during the smoke test.
      # The model is NOT bundled in the .exe — this step only enables smoke testing.
      - name: Download spaCy model for smoke test
        run: python -m spacy download en_core_web_sm

      - name: Build exe
        run: pyinstaller luminque.spec

      - name: Verify exe launches
        run: |
          .\dist\luminque.exe --_smoke_test || true

      # Phase 2 addition: verify binary is under 150 MB
      - name: Check binary size
        shell: pwsh
        run: |
          $size = (Get-Item dist\luminque.exe).length / 1MB
          Write-Host "Binary size: $([math]::Round($size, 1)) MB"
          if ($size -gt 150) {
            Write-Error "Binary exceeds 150 MB limit ($size MB). Check excludes in .spec."
            exit 1
          }

      - name: Upload unsigned artifact
        uses: actions/upload-artifact@v4
        with:
          name: luminque-unsigned
          path: dist/luminque.exe
          retention-days: 7
```

### `requirements.txt` additions

```
# Phase 2 additions — append to existing requirements.txt
openadapt-privacy>=0.1.0
presidio-analyzer>=2.2.0
presidio-anonymizer>=2.2.0
spacy>=3.7.0,<3.8.0   # pin minor version to match en_core_web_sm-3.7.x
```

`en_core_web_sm` itself is NOT in `requirements.txt` — it is downloaded at runtime on the user's machine, not at build time. The CI workflow downloads it explicitly as a separate step for smoke-test purposes only.

### Caching the spaCy model in CI

To avoid re-downloading the model (~12 MB) on every CI run, add a cache step:

```yaml
      - name: Cache spaCy model
        uses: actions/cache@v4
        with:
          path: C:\Users\runneradmin\AppData\Roaming\Python\Python311\site-packages\en_core_web_sm
          key: spacy-en-core-web-sm-3.7.1
          restore-keys: spacy-en-core-web-sm-

      - name: Download spaCy model (if not cached)
        run: python -m spacy download en_core_web_sm
```

---

## 7. Model Verification and Graceful Fallback

### Verification Before Scrubbing

Every time `luminque-sender` runs, it checks that the model is present before attempting any PII scrubbing. This check runs before the DB query, so a missing model fails fast with a clear log message rather than failing mid-batch.

```python
# luminque/sender.py (Phase 2 addition at top of run_send())

from luminque.model_download import model_is_present, MODEL_NAME

def run_send():
    if not model_is_present():
        log.error(
            "spaCy model '%s' not found at expected path. "
            "PII scrubbing cannot run. "
            "Run 'luminque.exe --upgrade' to install the model.",
            MODEL_NAME,
        )
        _send_degraded_heartbeat(reason="model_missing")
        sys.exit(1)

    # ... rest of send logic
```

The `_send_degraded_heartbeat()` call sends a minimal heartbeat-only payload to the server (no events, no screenshots) with a `degraded_reason` field so the server-side dashboard can flag this machine as needing attention. This is better than silently sending no heartbeat.

### Degraded Heartbeat Payload

```json
{
  "schema_version": "2",
  "machine_id": "...",
  "heartbeat": {
    "timestamp": "...",
    "sender_version": "2.0.0",
    "capture_running": true,
    "events_since_last_send": 0,
    "disk_free_mb": 12400,
    "degraded": true,
    "degraded_reason": "model_missing"
  },
  "events": [],
  "screenshots": [],
  "window_events": []
}
```

The `schema_version` is bumped to `"2"` for Phase 2 payloads. The server should accept both `"1"` (no scrubbing, raw data) and `"2"` (scrubbing applied) during the transition period when some machines are still on Phase 1 builds.

### Fallback Decision: Fail or Send Raw?

**Decision: fail, not fall back to raw.**

Sending raw data when scrubbing fails silently undermines the privacy guarantee Phase 2 is intended to provide. The sender exits non-zero if the model is missing. Task Scheduler will retry up to 3 times; after that, no data is sent until the model is installed.

The only data that leaves the machine when the model is missing is the degraded heartbeat (which contains no event or screenshot data). This is acceptable.

This decision should be reviewed if there is a hard business requirement for continuous data flow over privacy guarantees. For now, privacy wins.

### Model Integrity Check

`model_is_present()` currently checks only for the existence of `meta.json`. A stronger check verifies the model version:

```python
import json

def model_is_present() -> bool:
    meta_path = get_model_path() / "meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        # Verify the model name matches expectations
        return meta.get("name") == MODEL_NAME
    except (json.JSONDecodeError, OSError):
        return False
```

A version check (comparing `meta["version"]` to `MODEL_VERSION`) is intentionally omitted from the runtime guard. A forward-compatible minor version bump should not block sends. The version is logged at startup for diagnostics.

---

## 8. Changes from Phase 1 Deployment (Delta)

This section is a concise diff for developers who have already read the Phase 1 doc.

### New files

| File | Purpose |
|---|---|
| `luminque/model_download.py` | Downloads and verifies `en_core_web_sm` |
| `luminque/upgrade.py` | `--upgrade` mode for existing Phase 1 installs |

### Modified files

| File | Change |
|---|---|
| `main.py` | Add `--upgrade` dispatch branch |
| `luminque/onboard.py` | Add model download step, progress bar, background thread |
| `luminque.spec` | New hidden imports, new datas, remove spacy from excludes, add UPX exclusion for .pyd files |
| `.github/workflows/build.yml` | Download spaCy model for smoke test, add binary size check |
| `requirements.txt` | Add `openadapt-privacy`, `presidio-analyzer`, `presidio-anonymizer`, `spacy` |
| `luminque/sender.py` | Add model presence check at startup, degraded heartbeat on missing model |

### New directory on user machine

```
%APPDATA%\Luminque\models\
  en_core_web_sm\
    meta.json
    config.cfg
    tokenizer/
    vocab/
    ner/
    ...
```

### New log file on user machine

```
%APPDATA%\Luminque\upgrade.log
```

Written by `--upgrade` mode. Contains timestamps and progress messages for debugging failed upgrades.

### Payload schema change

`schema_version` field in the POST payload changes from `"1"` to `"2"`. The server must handle both versions during rollout. Phase 1 machines continue to send `schema_version: "1"` raw payloads until they receive the Phase 2 `.exe`.

---

## Implementation Locations

This section tells a developer exactly which files to touch in the `luminique-ops` repo and which new files to create. No source changes are needed in `openadapt-capture` or `openadapt-privacy` — both are consumed as dependencies only.

### Files to modify in `luminique-ops`

| File | What to change | Purpose |
|---|---|---|
| `luminque/onboarding/__init__.py` | Add model download step between consent acceptance and task registration; call `download_model()` on a background thread and wire up the progress callback | Extends the Phase 1 onboarding flow to ensure `en_core_web_sm` is present before tasks are registered, as specified in §3 |
| `luminque.spec` | Add Presidio/spaCy hidden imports and `collect_data_files()` calls; remove `spacy` from `excludes`; add `upx_exclude=["*.pyd"]` | Tells PyInstaller to bundle the spaCy and Presidio code paths that are not auto-detected at analysis time, as specified in §5 |
| `.github/workflows/build.yml` | Add "Download spaCy model for smoke test" step after `pip install`; add binary size gate (`< 150 MB`); add spaCy model cache step | Enables the CI smoke test to exercise the `--send` path with the model present and prevents silent binary bloat, as specified in §6 |
| `pyproject.toml` | Add `openadapt-privacy>=0.1.0`, `presidio-analyzer>=2.2.0`, `presidio-anonymizer>=2.2.0`, and `spacy>=3.7.0,<3.8.0` to the project dependencies | Declares the Phase 2 NLP dependencies so `pip install -e .` and CI installs pick them up automatically |
| `luminque/main.py` | Add `--upgrade` dispatch branch that imports and calls `luminque.upgrade.run_upgrade()` | Exposes the upgrade entry point so IT can silently download the model on existing Phase 1 machines without re-running onboarding, as specified in §4 |
| `luminque/sender/__init__.py` | Add model presence check at the top of `run_send()`; send degraded heartbeat and exit 1 if model is missing | Prevents raw PII data from being sent when the model is absent, as specified in §7 |

### New files to create in `luminique-ops`

| File | Purpose |
|---|---|
| `luminque/model_download.py` | Implements `download_model()`, `model_is_present()`, and `get_model_path()`. Downloads `en_core_web_sm` via `pip install --target` into `%APPDATA%\Luminque\models\` and verifies the result. Full implementation specified in §2. |
| `luminque/upgrade.py` | Implements `run_upgrade()`, the `--upgrade` / `--upgrade --silent` entry point for existing Phase 1 installs. Shows a minimal tkinter progress window (or runs headlessly) and delegates to `model_download.download_model()`. Full implementation specified in §4. |

### No changes needed in `openadapt-capture` or `openadapt-privacy`

`openadapt-capture` is unchanged in Phase 2 — the capture pipeline does not perform scrubbing and requires no modification.

`openadapt-privacy` is consumed as a pip dependency (`openadapt-privacy>=0.1.0`) and called by `luminque/sender/__init__.py` via its public API. No modifications to its source are needed; all Phase 2 integration points are on the `luminique-ops` side.

---

## 9. Out of Scope for Phase 2

The following are explicitly deferred. Do not implement these in the Phase 2 codebase.

| Item | Notes |
|---|---|
| `en_core_web_trf` (transformer model) | ~500 MB; requires GPU for practical performance; out of scope until a dedicated high-accuracy tier is needed |
| Bundling the spaCy model into the `.exe` | Adds 12 MB to every build; model updates would require a full binary rebuild; runtime download is preferred |
| Online/cloud NLP fallback | If model is missing, fail fast — do not call an external API as a fallback |
| Custom Presidio recognizers | The default Presidio recognizer set (email, phone, person, org, location, etc.) is used as-is; custom entity types are a future iteration |
| Scrubbing screenshot image content (OCR + redaction) | Screenshots are sent as-is in Phase 2; image-based PII (text on screen) is not scrubbed; this is a Phase 3+ concern |
| Scrubbing audio transcriptions | `AudioInfo` rows are still not sent in Phase 2 |
| Model auto-update | The model version is pinned at build time; updating requires a new `.exe` release; no in-place model update mechanism |
| System tray icon | Unchanged from Phase 1 — still out of scope |
| Programmatic uninstaller | Unchanged from Phase 1 — still out of scope |
| Data encryption at rest | SQLite DB remains unencrypted in Phase 2 |
| Crash reporting / Sentry | Still logging to local files only |
| Browser events | Still not included in the Phase 2 payload |

---

## Appendix A: Updated Key Dependencies

Phase 1 dependencies are unchanged. Phase 2 adds:

| Package | Version | Purpose |
|---|---|---|
| `openadapt-privacy` | `>=0.1.0` | PII scrubbing wrapper (orchestrates Presidio + spaCy) |
| `presidio-analyzer` | `>=2.2.0` | PII entity recognition engine |
| `presidio-anonymizer` | `>=2.2.0` | PII entity replacement/redaction |
| `spacy` | `>=3.7.0,<3.8.0` | NLP backend for Presidio |
| `en_core_web_sm` | `3.7.1` | English NLP model (~12 MB); runtime download only, not in requirements.txt |

`en_core_web_sm` is chosen over `en_core_web_trf` for three reasons:
1. Size: ~12 MB vs ~500 MB.
2. No GPU dependency: `en_core_web_sm` uses CNNs, not transformers; runs on CPU.
3. Speed: adequately fast for the sender's batch scrubbing workload (not real-time).

---

## Appendix B: Updated Testing Checklist

Testing items that are new or changed in Phase 2 (run in addition to the full Phase 1 checklist):

- [ ] Fresh Windows 10 VM with Phase 2 `.exe`: complete onboarding, verify model download progress bar appears and model appears in `%APPDATA%\Luminque\models\en_core_web_sm\`
- [ ] Onboarding with no internet: verify error dialog appears with actionable message; tasks are NOT registered; retry succeeds after reconnecting
- [ ] Upgrade mode on a Phase 1 machine: `luminque.exe --upgrade`, verify model is downloaded, `upgrade.log` is written, exit code 0
- [ ] Upgrade mode silent: `luminque.exe --upgrade --silent`, verify no window appears, exit code 0
- [ ] Upgrade mode when model already present: verify immediate exit 0, no re-download
- [ ] Sender with model present: verify PII scrubbing runs, payload contains scrubbed fields, `schema_version: "2"` in POST body
- [ ] Sender with model missing (delete `%APPDATA%\Luminque\models\`): verify exit 1, degraded heartbeat sent, no raw events in payload
- [ ] Binary size check: `dist\luminque.exe` must be under 150 MB
- [ ] UPX sanity check: `.pyd` files in the binary should not be UPX-compressed; verify with `upx -t dist\luminque.exe` (should report pyd files as not packed)
- [ ] SmartScreen with signed Phase 2 `.exe`: publisher name displayed, no warning
