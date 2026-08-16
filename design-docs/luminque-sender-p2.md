# luminque-sender Phase 2 — Technical Design Document

**Status:** Draft  
**Date:** 2026-05-05  
**Scope:** Phase 2 — client-side PII scrubbing before transmission

---

## 1. Overview

Phase 2 adds client-side PII scrubbing to the sender process. Sensitive data is redacted before it leaves the user's machine. The server never receives raw keystrokes, window text, or screenshots containing personal information.

### What Changes From Phase 1

| Area | Phase 1 | Phase 2 |
|---|---|---|
| Text fields | Sent raw | Scrubbed via Presidio NER before serialization |
| Screenshots | Sent raw | Selectively scrubbed based on window title pattern matching |
| Payload schema version | `"1"` | `"2"` |
| spaCy model | Not required | `en_core_web_sm` (downloaded during onboarding) |
| New module | — | `scrubbing.py`, `capture_loader.py` |
| Sender version | `1.x.x` | `2.0.0` |

### What Stays the Same

Everything else is unchanged: cursor tracking, state file, gzip+HTTPS transport, heartbeat, credentials management, Task Scheduler lifecycle, retry strategy, 24h retention cap, and the Phase 1 payload structure for non-text fields. Phase 2 is additive — it inserts a scrubbing step into the existing flow rather than replacing the flow.

---

## 2. Implementation Locations

All changes are confined to `luminque-ops`. Neither `openadapt-capture` nor `openadapt-privacy` source is modified — both are consumed as read-only pip dependencies.

### Phase 2 additions

| Component | File | Type | Change |
|---|---|---|---|
| `CaptureRecordingLoader` | `luminque/sender/capture_loader.py` | New file | Maps SQLAlchemy `ActionEvent`/`Screenshot`/`WindowEvent` rows to `openadapt-privacy` `Action`/`Screenshot`/`Recording` dataclasses; also provides `scrubbed_action_to_dict` and `scrubbed_screenshot_to_dict` to convert scrubbed dataclasses back to the Phase 1 payload dict shape |
| Scrubbing pipeline | `luminque/sender/scrubbing.py` | New file | Top-level `scrub_batch` entry point; owns the module-level `PresidioScrubbingProvider` singleton (`get_scrubber`); also contains `scrub_window_event` for scrubbing `WindowEvent` dicts before serialization |
| Window title filter | `luminque/sender/window_filter.py` | New file | `is_sensitive_window` and `get_scrub_screenshot_ids` — decides which screenshots need image scrubbing by matching `WindowEvent.title` against `_SENSITIVE_PATTERNS` (browsers, email clients, password managers, HR/payroll, CRM, finance, healthcare, generic PII keywords) |
| spaCy model guard | `luminque/sender/model.py` | New file | `is_model_available` and `require_spacy_model` — aborts the sender with exit code 2 if `en_core_web_sm` is not installed, preventing unscrubbed transmission |

### Phase 1 files that must be modified to wire in scrubbing

| Component | File | Type | Change |
|---|---|---|---|
| Orchestration | `luminque/sender/sender.py` | Modify (Phase 1 file) | Insert `require_spacy_model()` and `scrub_batch()` call between `query_batch` and `build_payload`; pass pre-scrubbed dicts to `build_payload` instead of raw SQLAlchemy objects |
| Payload assembly | `luminque/sender/payload.py` | Modify (Phase 1 file) | Update `build_payload` signature to accept `list[dict]` for `action_events` and `screenshots` (scrubbed dicts replace raw model objects); change `schema_version` from `"1"` to `"2"` |
| Constants | `luminque/sender/constants.py` | Modify (Phase 1 file) | Add `SPACY_MODEL_NAME = "en_core_web_sm"`, `MAX_IMAGES_TO_SCRUB_PER_CYCLE = 50`; bump `SENDER_VERSION` to `"2.0.0"` and `PAYLOAD_SCHEMA_VERSION` to `"2"` |
| Package entry point | `luminque/sender/__init__.py` | Modify (Phase 1 stub) | Replace the `print("not implemented yet")` stub with the real `run_sender()` call (this replacement was already required by Phase 1; Phase 2 does not change the stub replacement itself) |

### `pyproject.toml` dependency addition

| Component | File | Type | Change |
|---|---|---|---|
| Privacy dependency | `pyproject.toml` | Modify | Add `"openadapt-privacy[presidio] @ git+https://github.com/OpenAdaptAI/openadapt-privacy"` and `"Pillow>=10.0"` to `[project] dependencies` |

---

## 3. Revised Sender Flow

The full sender flow from Phase 1 (Section 2 of `luminque-sender-p1.md`) is unchanged except for the addition of step 4a.

```
1.  Load state
2.  Open capture DB (read-only)
3.  Query new events       SELECT ActionEvent, Screenshot, WindowEvent
                           WHERE id > last_sent_action_event_id LIMIT 5000
4.  Collect heartbeat
4a. [NEW] Scrub batch      For each ActionEvent: scrub text fields
                           For each Screenshot: scrub image IF window title
                           matches a sensitive pattern
5.  Build payload          Assemble JSON — now contains scrubbed data
6.  Gzip payload
7.  POST to server
8.  Handle response
9.  Enforce retention cap
10. Exit 0
```

Step 4a runs in-memory. The scrubbed data never touches the local DB. The DB always contains the original captured data.

---

## 4. CaptureRecordingLoader

`RecordingLoader` in `openadapt-privacy` is abstract. No SQLite implementation exists in the library. Phase 2 must provide `CaptureRecordingLoader`, which reads from `openadapt-capture`'s `recording.db` and maps its SQLAlchemy models into `openadapt-privacy`'s `Action`/`Screenshot`/`Recording` dataclasses.

### Why a Loader Is Needed

The scrubbing entry point in `openadapt-privacy` is `Action.scrub(scrubber)` (from `loaders.py`). This method operates on the `Action` dataclass, not on `openadapt_capture.db.models.ActionEvent`. A mapping layer is required to translate between the two.

The loader is also the right place to make the image-vs-no-image decision for screenshots — it can skip loading PNG bytes for screenshots that will not be scrubbed, which avoids deserializing large blobs unnecessarily.

### Model Mapping

The table below documents how each `openadapt-capture` SQLAlchemy field maps to the `openadapt-privacy` dataclass fields.

**ActionEvent → Action**

| Capture model field | Privacy dataclass field | Notes |
|---|---|---|
| `ActionEvent.id` | `Action.id` | Direct |
| `ActionEvent.name` | `Action.action_type` | Direct (`"click"`, `"type"`, etc.) |
| `ActionEvent.timestamp` | `Action.timestamp` | Direct (Unix float) |
| `ActionEvent.key_char` | `Action.text` | Key character typed; `is_separated=True` when scrubbing |
| `ActionEvent.canonical_key_char` | `Action.canonical_text` | Canonical form; `is_separated=True` when scrubbing |
| `ActionEvent.window_event.title` | `Action.window_title` | Via join/relationship; may be None |
| `ActionEvent.element_state` | `Action.metadata` | JSON dict from DB; scrub_dict applies SCRUB_KEYS_HTML |
| `ActionEvent.screenshot_id` | `Action.screenshot_id` | Direct |

Fields with no privacy-dataclass equivalent (`mouse_x`, `mouse_y`, `mouse_button_name`, `key_name`, `key_vk`, `recording_id`, `parent_id`, `disabled`) are carried in `Action.metadata` as a sub-dict under the key `"capture_fields"`. These are not scrubbed (no PII risk in coordinates, key names, or booleans) and are passed through to serialization unchanged.

**Screenshot → Screenshot (privacy dataclass)**

| Capture model field | Privacy dataclass field | Notes |
|---|---|---|
| `Screenshot.id` | `Screenshot.id` | Direct |
| `Screenshot.id` | `Screenshot.action_id` | Approximation — the privacy dataclass links to action; we use screenshot id here since capture links screenshots to action_events via `ActionEvent.screenshot_id`, not the reverse. The loader passes the first `ActionEvent.id` that references this screenshot. |
| `Screenshot.timestamp` | `Screenshot.timestamp` | Direct |
| `Screenshot.image` (PIL property) | `Screenshot.image` | Load only for screenshots that will be scrubbed |
| `None` | `Screenshot.path` | Not used; images are held in memory |

### CaptureRecordingLoader Class

```python
# luminque_sender/capture_loader.py

from __future__ import annotations

import io
import logging
from typing import Optional

from PIL import Image
from sqlalchemy.orm import Session

from openadapt_capture.db.models import ActionEvent, Screenshot as CaptureScreenshot
from openadapt_capture.db.models import WindowEvent
from openadapt_privacy.loaders import (
    Action,
    Recording,
    RecordingLoader,
    Screenshot as PrivacyScreenshot,
)

logger = logging.getLogger(__name__)


class CaptureRecordingLoader(RecordingLoader):
    """Load a batch of capture DB rows into openadapt-privacy Recording dataclasses.

    This is NOT a full RecordingLoader in the sense of loading an entire
    recording from disk. It operates on a pre-queried batch of SQLAlchemy
    model instances — the query has already been executed by db.py.

    The `load` and `save` abstract methods are implemented as no-ops;
    the real entry point is `from_batch`.
    """

    def load(self, source: str) -> Recording:
        raise NotImplementedError("Use from_batch() for capture DB batches.")

    def save(self, recording: Recording, destination: str) -> None:
        raise NotImplementedError("CaptureRecordingLoader is read-only.")

    def from_batch(
        self,
        action_events: list[ActionEvent],
        capture_screenshots: list[CaptureScreenshot],
        window_events: list[WindowEvent],
        scrub_screenshot_ids: set[int],
    ) -> Recording:
        """Build a Recording from a queried batch.

        Args:
            action_events: ActionEvent rows from the current batch.
            capture_screenshots: Screenshot rows referenced by the batch.
            window_events: WindowEvent rows referenced by the batch.
            scrub_screenshot_ids: Set of Screenshot.id values whose images
                should be loaded into PIL for scrubbing. All others are
                loaded without image data (image=None).

        Returns:
            A Recording whose actions and screenshots are ready for scrubbing.
        """
        # Build lookup maps
        window_map: dict[int, WindowEvent] = {w.id: w for w in window_events}
        # Map screenshot_id -> first action_id that references it
        screenshot_action_map: dict[int, int] = {}
        for ae in action_events:
            if ae.screenshot_id and ae.screenshot_id not in screenshot_action_map:
                screenshot_action_map[ae.screenshot_id] = ae.id

        actions = [
            self._map_action_event(ae, window_map)
            for ae in action_events
        ]

        screenshots = [
            self._map_screenshot(
                s,
                action_id=screenshot_action_map.get(s.id, s.id),
                load_image=(s.id in scrub_screenshot_ids),
            )
            for s in capture_screenshots
        ]

        return Recording(
            id=None,
            task_description=None,
            timestamp=action_events[0].timestamp if action_events else None,
            actions=actions,
            screenshots=screenshots,
            metadata={},
        )

    def _map_action_event(
        self,
        ae: ActionEvent,
        window_map: dict[int, WindowEvent],
    ) -> Action:
        """Map a single ActionEvent to an Action dataclass."""
        window_title: Optional[str] = None
        if ae.window_event_id and ae.window_event_id in window_map:
            window_title = window_map[ae.window_event_id].title

        # Fields with PII risk go into named Action fields.
        # All other capture-specific fields are stashed in metadata
        # so they survive the scrubbing round-trip and can be re-serialized.
        capture_fields = {
            "name": ae.name,
            "recording_id": ae.recording_id,
            "recording_timestamp": ae.recording_timestamp,
            "screenshot_id": ae.screenshot_id,
            "window_event_id": ae.window_event_id,
            "mouse_x": ae.mouse_x,
            "mouse_y": ae.mouse_y,
            "mouse_dx": ae.mouse_dx,
            "mouse_dy": ae.mouse_dy,
            "mouse_button_name": ae.mouse_button_name,
            "mouse_pressed": ae.mouse_pressed,
            "key_name": ae.key_name,
            "key_vk": ae.key_vk,
            "canonical_key_name": ae.canonical_key_name,
            "canonical_key_vk": ae.canonical_key_vk,
            "active_segment_description": ae.active_segment_description,
            "parent_id": ae.parent_id,
            "disabled": ae.disabled,
        }

        # element_state is a JSON dict and IS scrubbed via scrub_dict
        metadata: dict = {"capture_fields": capture_fields}
        if ae.element_state:
            metadata["element_state"] = ae.element_state

        return Action(
            id=ae.id,
            action_type=ae.name or "unknown",
            timestamp=ae.timestamp,
            text=ae.key_char,
            canonical_text=ae.canonical_key_char,
            window_title=window_title,
            element_text=None,  # Not a separate field in capture models
            metadata=metadata,
            screenshot_id=ae.screenshot_id,
        )

    def _map_screenshot(
        self,
        s: CaptureScreenshot,
        action_id: int,
        load_image: bool,
    ) -> PrivacyScreenshot:
        """Map a capture Screenshot to a privacy Screenshot dataclass."""
        image: Optional[Image.Image] = None
        if load_image and s.png_data:
            image = Image.open(io.BytesIO(s.png_data))

        return PrivacyScreenshot(
            id=s.id,
            action_id=action_id,
            timestamp=s.timestamp,
            image=image,
            path=None,
        )
```

### Round-Trip Back to Serializable Dicts

After scrubbing, the scrubbed `Recording` must be converted back to the dict shape that `payload.py` serializes. A companion function handles this:

```python
# luminque_sender/capture_loader.py (continued)

import base64

from openadapt_capture.db.models import Screenshot as CaptureScreenshot
from openadapt_privacy.loaders import Action, Recording, Screenshot as PrivacyScreenshot


def scrubbed_action_to_dict(action: Action) -> dict:
    """Convert a scrubbed Action back to the Phase 1 ActionEvent dict shape.

    The capture_fields stashed in metadata are promoted back to top-level
    keys. The scrubbed text fields (key_char, canonical_key_char) overwrite
    their originals.
    """
    cf = action.metadata.get("capture_fields", {})
    return {
        "id": action.id,
        "name": cf.get("name"),
        "timestamp": action.timestamp,
        "recording_timestamp": cf.get("recording_timestamp"),
        "recording_id": cf.get("recording_id"),
        "screenshot_id": cf.get("screenshot_id"),
        "window_event_id": cf.get("window_event_id"),
        "mouse_x": cf.get("mouse_x"),
        "mouse_y": cf.get("mouse_y"),
        "mouse_dx": cf.get("mouse_dx"),
        "mouse_dy": cf.get("mouse_dy"),
        "mouse_button_name": cf.get("mouse_button_name"),
        "mouse_pressed": cf.get("mouse_pressed"),
        "key_name": cf.get("key_name"),
        # Scrubbed values replace originals:
        "key_char": action.text,
        "key_vk": cf.get("key_vk"),
        "canonical_key_name": cf.get("canonical_key_name"),
        "canonical_key_char": action.canonical_text,
        "canonical_key_vk": cf.get("canonical_key_vk"),
        "active_segment_description": cf.get("active_segment_description"),
        "available_segment_descriptions": None,
        "parent_id": cf.get("parent_id"),
        "element_state": action.metadata.get("element_state"),
        "disabled": cf.get("disabled", False),
    }


def scrubbed_screenshot_to_dict(
    privacy_screenshot: PrivacyScreenshot,
    original: CaptureScreenshot,
) -> dict:
    """Convert a scrubbed Screenshot back to the Phase 1 screenshot dict shape.

    If the screenshot was scrubbed (image is not None on the privacy dataclass),
    re-encode the scrubbed PIL image to PNG bytes for transmission.
    If it was not scrubbed, fall back to original png_data bytes.
    """
    if privacy_screenshot.image is not None:
        import io as _io
        buf = _io.BytesIO()
        privacy_screenshot.image.save(buf, format="PNG")
        png_bytes = buf.getvalue()
    else:
        png_bytes = original.png_data

    return {
        "id": original.id,
        "recording_id": original.recording_id,
        "recording_timestamp": original.recording_timestamp,
        "timestamp": original.timestamp,
        "png_data_b64": base64.b64encode(png_bytes).decode("ascii") if png_bytes else None,
        "png_diff_data_b64": (
            base64.b64encode(original.png_diff_data).decode("ascii")
            if original.png_diff_data else None
        ),
        "png_diff_mask_data_b64": (
            base64.b64encode(original.png_diff_mask_data).decode("ascii")
            if original.png_diff_mask_data else None
        ),
    }
```

---

## 5. Scrubbing Pipeline

### Placement in the Flow

Scrubbing runs **before serialization**, operating on the raw SQLAlchemy objects translated into `Action`/`Screenshot` dataclasses. Serialization (`build_payload`) then operates on the already-scrubbed dicts. This means:

- The server never receives raw sensitive text.
- The local DB is not modified.
- The scrubbed state only exists in process memory during a single sender invocation.

### New Module: scrubbing.py

```python
# luminque_sender/scrubbing.py

from __future__ import annotations

import logging
from typing import Optional

from openadapt_capture.db.models import ActionEvent
from openadapt_capture.db.models import Screenshot as CaptureScreenshot
from openadapt_capture.db.models import WindowEvent
from openadapt_privacy.loaders import Recording
from openadapt_privacy.providers.presidio import PresidioScrubbingProvider

from luminque_sender.capture_loader import (
    CaptureRecordingLoader,
    scrubbed_action_to_dict,
    scrubbed_screenshot_to_dict,
)
from luminque_sender.window_filter import get_scrub_screenshot_ids

logger = logging.getLogger(__name__)


def scrub_batch(
    action_events: list[ActionEvent],
    capture_screenshots: list[CaptureScreenshot],
    window_events: list[WindowEvent],
    scrubber: PresidioScrubbingProvider,
) -> tuple[list[dict], list[dict]]:
    """Scrub a batch of capture events in-memory.

    Text scrubbing runs on every ActionEvent. Image scrubbing runs only on
    screenshots whose associated window title matches a sensitive pattern.

    Args:
        action_events: Raw ActionEvent rows from the DB.
        capture_screenshots: Raw Screenshot rows for this batch.
        window_events: Raw WindowEvent rows for this batch.
        scrubber: Initialized PresidioScrubbingProvider.

    Returns:
        Tuple of (scrubbed_event_dicts, scrubbed_screenshot_dicts) ready
        for payload assembly.
    """
    window_map = {w.id: w for w in window_events}
    scrub_screenshot_ids = get_scrub_screenshot_ids(action_events, window_map)

    logger.info(
        f"Scrubbing {len(action_events)} events; "
        f"{len(scrub_screenshot_ids)}/{len(capture_screenshots)} screenshots "
        f"flagged for image scrubbing."
    )

    loader = CaptureRecordingLoader()
    recording = loader.from_batch(
        action_events=action_events,
        capture_screenshots=capture_screenshots,
        window_events=window_events,
        scrub_screenshot_ids=scrub_screenshot_ids,
    )

    # Text scrubbing always runs; image scrubbing is selective via Recording.scrub
    # Recording.scrub(scrub_images=True) will call screenshot.scrub() for each
    # screenshot in the recording. Since we only loaded PIL images for
    # screenshots in scrub_screenshot_ids, screenshots without images are
    # passed through unchanged by Screenshot.scrub().
    scrubbed_recording: Recording = recording.scrub(scrubber, scrub_images=True)

    # Build lookup for original screenshot objects (needed for metadata fields)
    original_screenshot_map = {s.id: s for s in capture_screenshots}

    scrubbed_action_dicts = [
        scrubbed_action_to_dict(action)
        for action in scrubbed_recording.actions
    ]

    scrubbed_screenshot_dicts = [
        scrubbed_screenshot_to_dict(
            privacy_screenshot=ps,
            original=original_screenshot_map[ps.id],
        )
        for ps in scrubbed_recording.screenshots
    ]

    return scrubbed_action_dicts, scrubbed_screenshot_dicts
```

### Integration Point in sender.py

In `sender.py`, after `query_batch` returns and before `build_payload`, add:

```python
# sender.py (Phase 2 delta)

from luminque_sender.scrubbing import scrub_batch
from luminque_sender.model import require_spacy_model

# --- after query_batch ---
require_spacy_model()   # Abort if model not present (see Section 7)

scrubber = get_scrubber()   # Module-level singleton, lazy-init (see below)

action_event_dicts, screenshot_dicts = scrub_batch(
    action_events=action_events,
    capture_screenshots=screenshots,
    window_events=window_events,
    scrubber=scrubber,
)

# --- then pass dicts directly to build_payload ---
payload = build_payload(
    machine_id=machine_id,
    heartbeat=heartbeat,
    action_event_dicts=action_event_dicts,
    screenshot_dicts=screenshot_dicts,
    window_event_dicts=[serialize_window_event(w) for w in window_events],
)
```

`build_payload` in Phase 2 accepts pre-serialized dicts instead of SQLAlchemy objects for events and screenshots, since scrubbing has already produced the final dict representation. `serialize_window_event` is unchanged — window events are serialized directly (their text is scrubbed as part of the ActionEvent's `window_title` field in the Action dataclass, but the `WindowEvent` dict itself is also scrubbed before inclusion — see Section 5).

### Scrubber Singleton

Presidio initialization (loading spaCy, building the NLP pipeline) takes 2–5 seconds. Initialize once per sender invocation:

```python
# luminque_sender/scrubbing.py

_scrubber: PresidioScrubbingProvider | None = None

def get_scrubber() -> PresidioScrubbingProvider:
    """Return the module-level scrubber, initializing it on first call."""
    global _scrubber
    if _scrubber is None:
        logger.info("Initializing Presidio scrubbing provider...")
        _scrubber = PresidioScrubbingProvider()
        logger.info("Presidio scrubber initialized.")
    return _scrubber
```

---

## 6. Text Scrubbing

### Fields Scrubbed

Text scrubbing is driven by `SCRUB_KEYS_HTML` from `openadapt_privacy.config`. The default set is:

```python
SCRUB_KEYS_HTML = [
    "text",           # key_char in Action.text
    "canonical_text", # canonical_key_char in Action.canonical_text
    "title",          # window title in Action.window_title
    "state",          # WindowEvent.state JSON (entire dict, scrub_all=True)
    "task_description",
    "key_char",
    "canonical_key_char",
    "key_vk",
    "children",
    "value",
    "tooltip",
]
```

The primary fields containing user-typed data in the capture models are:

| Capture field | Privacy field | Scrubbed as |
|---|---|---|
| `ActionEvent.key_char` | `Action.text` | `scrub_text(text, is_separated=True)` |
| `ActionEvent.canonical_key_char` | `Action.canonical_text` | `scrub_text(canonical_text, is_separated=True)` |
| `ActionEvent.element_state` | `Action.metadata["element_state"]` | `scrub_dict(element_state)` — recursively applies SCRUB_KEYS_HTML |
| `WindowEvent.title` | `Action.window_title` | `scrub_text(window_title)` |
| `WindowEvent.state` | Serialized separately | `scrub_dict(state, scrub_all=True)` (see below) |

### is_separated=True for Key Sequences

`key_char` and `canonical_key_char` store individual keystrokes. In the context of openadapt-privacy, these are "separated" text — the user typed `j`, `o`, `h`, `n` as separate events, not the word `john` as a single string.

When `is_separated=True`, the Presidio provider joins the separated characters before NER analysis (removes the `-` separator used in key sequence notation like `j-o-h-n`), runs inference on the joined string, and then re-applies the separator format to the result. This enables detection of names and emails that were typed one character at a time.

```python
# From PresidioScrubbingProvider.scrub_text (presidio.py, lines 136-166)
# The separator is config.ACTION_TEXT_SEP = "-"
# Text like "j-o-h-n" is joined to "john", analyzed, then re-separated.
```

**Important:** Key sequences that are wrapped in angle brackets (e.g., `<ctrl>`, `<shift>`) are detected by `startswith("<")` / `endswith(">")` and are passed through without joining — they are control key names, not user-typed content.

### WindowEvent.state Scrubbing

`WindowEvent.state` is a JSON column containing the full accessibility tree of the active window at the time of the event. It can contain field labels, input values, and UI text. It is scrubbed with `scrub_all=True` (from `TextScrubbingMixin.scrub_dict`, line 188 in `base.py`) which runs `scrub_text` on every string value regardless of key name.

In Phase 2, window events are scrubbed separately before serialization:

```python
# luminque_sender/scrubbing.py

from openadapt_privacy.pipelines.dicts import scrub_dict

def scrub_window_event(w: WindowEvent, scrubber: PresidioScrubbingProvider) -> dict:
    """Serialize and scrub a WindowEvent dict."""
    raw = {
        "id": w.id,
        "recording_id": w.recording_id,
        "recording_timestamp": w.recording_timestamp,
        "timestamp": w.timestamp,
        "title": w.title,
        "left": w.left,
        "top": w.top,
        "width": w.width,
        "height": w.height,
        "window_id": w.window_id,
        "state": w.state,
    }
    return scrub_dict(raw, scrubber)
```

`scrub_dict` will encounter the `"state"` key and apply `scrub_all=True` to it (via the `elif isinstance(key, str) and key == "state"` branch in `TextScrubbingMixin.scrub_dict`, line 187–188 of `base.py`). The `"title"` key is in `SCRUB_KEYS_HTML`, so it is also scrubbed.

---

## 7. Selective Image Scrubbing

Image scrubbing (OCR + redaction via Presidio Image Redactor) is expensive: 1–3 seconds per screenshot on a typical user laptop. Scrubbing every screenshot in a 5000-event batch would be prohibitive. Phase 2 scrubs only screenshots where the associated window is considered sensitive.

### Window Title Pattern Matching

The decision is made per-screenshot, based on the `WindowEvent.title` of the window that was active when the screenshot was taken. The title is matched against a list of regex patterns.

```python
# luminque_sender/window_filter.py

from __future__ import annotations

import re
import logging
from typing import Optional

from openadapt_capture.db.models import ActionEvent, WindowEvent

logger = logging.getLogger(__name__)

# Compiled once at module load time.
# Each entry is a (human_label, compiled_regex) pair for logging.
_SENSITIVE_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Browsers — any browser tab could show a web form with PII
    ("browser_chrome",      re.compile(r"\bGoogle Chrome\b", re.IGNORECASE)),
    ("browser_firefox",     re.compile(r"\bMozilla Firefox\b", re.IGNORECASE)),
    ("browser_edge",        re.compile(r"\bMicrosoft Edge\b", re.IGNORECASE)),
    ("browser_safari",      re.compile(r"\bSafari\b", re.IGNORECASE)),
    ("browser_brave",       re.compile(r"\bBrave\b", re.IGNORECASE)),
    ("browser_opera",       re.compile(r"\bOpera\b", re.IGNORECASE)),
    # Email clients
    ("email_outlook",       re.compile(r"\bOutlook\b", re.IGNORECASE)),
    ("email_thunderbird",   re.compile(r"\bThunderbird\b", re.IGNORECASE)),
    ("email_gmail_web",     re.compile(r"Gmail", re.IGNORECASE)),
    ("email_apple_mail",    re.compile(r"\bMail\b.*Apple", re.IGNORECASE)),
    # Password managers
    ("pm_1password",        re.compile(r"1Password", re.IGNORECASE)),
    ("pm_lastpass",         re.compile(r"LastPass", re.IGNORECASE)),
    ("pm_bitwarden",        re.compile(r"Bitwarden", re.IGNORECASE)),
    ("pm_dashlane",         re.compile(r"Dashlane", re.IGNORECASE)),
    ("pm_keepass",          re.compile(r"KeePass", re.IGNORECASE)),
    # HR and payroll systems
    ("hr_workday",          re.compile(r"Workday", re.IGNORECASE)),
    ("hr_bamboohr",         re.compile(r"BambooHR", re.IGNORECASE)),
    ("hr_adp",              re.compile(r"\bADP\b", re.IGNORECASE)),
    ("hr_successfactors",   re.compile(r"SuccessFactors", re.IGNORECASE)),
    ("hr_paychex",          re.compile(r"Paychex", re.IGNORECASE)),
    ("hr_gusto",            re.compile(r"\bGusto\b", re.IGNORECASE)),
    # CRM / contact systems
    ("crm_salesforce",      re.compile(r"Salesforce", re.IGNORECASE)),
    ("crm_hubspot",         re.compile(r"HubSpot", re.IGNORECASE)),
    # Finance
    ("finance_quickbooks",  re.compile(r"QuickBooks", re.IGNORECASE)),
    ("finance_xero",        re.compile(r"\bXero\b", re.IGNORECASE)),
    # Healthcare
    ("health_epic",         re.compile(r"\bEpic\b", re.IGNORECASE)),
    ("health_cerner",       re.compile(r"\bCerner\b", re.IGNORECASE)),
    # Generic PII-bearing window title keywords
    ("generic_login",       re.compile(r"\b(sign.?in|log.?in|login|password)\b", re.IGNORECASE)),
    ("generic_personal",    re.compile(r"\b(profile|account|settings|preferences)\b", re.IGNORECASE)),
    ("generic_payment",     re.compile(r"\b(payment|checkout|billing|credit.?card)\b", re.IGNORECASE)),
]


def is_sensitive_window(title: Optional[str]) -> tuple[bool, Optional[str]]:
    """Check whether a window title matches any sensitive pattern.

    Args:
        title: Window title string (may be None).

    Returns:
        (is_sensitive, matched_label) — label is None if not sensitive.
    """
    if not title:
        return False, None
    for label, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(title):
            return True, label
    return False, None


def get_scrub_screenshot_ids(
    action_events: list[ActionEvent],
    window_map: dict[int, WindowEvent],
) -> set[int]:
    """Return the set of screenshot IDs that should have image scrubbing applied.

    A screenshot is included if any ActionEvent that references it was
    associated with a sensitive window title.

    Args:
        action_events: ActionEvent rows from the current batch.
        window_map: Dict mapping window_event_id -> WindowEvent.

    Returns:
        Set of Screenshot.id values requiring image scrubbing.
    """
    scrub_ids: set[int] = set()
    for ae in action_events:
        if not ae.screenshot_id:
            continue
        title: Optional[str] = None
        if ae.window_event_id and ae.window_event_id in window_map:
            title = window_map[ae.window_event_id].title
        sensitive, label = is_sensitive_window(title)
        if sensitive:
            logger.debug(
                f"Screenshot {ae.screenshot_id} flagged for image scrubbing "
                f"(window: {title!r}, pattern: {label})"
            )
            scrub_ids.add(ae.screenshot_id)
    return scrub_ids
```

### Extending the Pattern List

To add new sensitive patterns, append to `_SENSITIVE_PATTERNS` in `window_filter.py`:

```python
("my_new_app", re.compile(r"MyApp Name", re.IGNORECASE)),
```

No other code changes are required. Patterns are compiled once at import time.

Future work (Phase 3+): move the pattern list to a config file or Windows Registry key so it can be updated without a full sender deployment.

---

## 8. Performance Considerations

### spaCy Model Choice

Phase 2 uses `en_core_web_sm` rather than `en_core_web_trf`.

| Model | Size | Inference speed | NER accuracy |
|---|---|---|---|
| `en_core_web_sm` | ~12 MB | ~5,000 tokens/sec (CPU) | Moderate — misses subtle PII |
| `en_core_web_trf` | ~500 MB | ~100–300 tokens/sec (CPU) | High |

On a typical user laptop (4-core, no GPU), `en_core_web_trf` would add 10–30 minutes to a 5000-event batch. `en_core_web_sm` keeps text scrubbing under 60 seconds for the same batch. The accuracy tradeoff is acceptable for Phase 2 — the primary PII risk (names, emails, phone numbers) is partially covered, and the model can be upgraded to `en_core_web_trf` in a later phase once per-user performance baselines are established.

To switch models, change `SPACY_MODEL_NAME` in `PrivacyConfig` and update the `SCRUB_CONFIG_TRF` dict:

```python
# For en_core_web_sm:
SCRUB_CONFIG_TRF = {
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
}
SPACY_MODEL_NAME = "en_core_web_sm"
```

Pass a custom config when constructing the scrubber (or patch the global `config` object before first use).

### Image Scrubbing Cost

Image scrubbing (OCR + NER on screenshot pixels) is significantly more expensive than text scrubbing — roughly 2–5 seconds per 1080p screenshot. Selective scrubbing (Section 6) limits this to only sensitive windows.

In practice, a typical 30–60 minute capture session on a business application should produce far fewer than 100 screenshots that match sensitive patterns. Budget 5–10 minutes for image scrubbing in worst-case scenarios. If image scrubbing consistently pushes cycles past 15 minutes, add an emergency cap:

```python
MAX_IMAGES_TO_SCRUB_PER_CYCLE = 50  # In constants.py
```

If the cap is hit, log a warning. The remaining screenshots are sent unscrubbed for that cycle (document this behavior clearly — it may need a policy decision).

### CPU Impact on User Machines

The sender is a scheduled task, not a real-time process. It runs while the user's machine is logged in but may be idle. To reduce CPU contention:

- Presidio initializes lazily — only when there are events to scrub.
- Use `os.nice(10)` (UNIX) / `SetThreadPriority(THREAD_PRIORITY_BELOW_NORMAL)` (Windows) if sender CPU usage is reported as disruptive.
- On Windows, the `psutil` process priority can be lowered at startup:

```python
import psutil, os
psutil.Process(os.getpid()).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
```

### Batching and NER Inference

Presidio does not natively batch-process a list of strings in a single NER call. Each `scrub_text` call is a separate inference pass. For 5000 events, this is 5000 individual calls. Given `en_core_web_sm` speeds, this is acceptable. If switching to `en_core_web_trf`, consider batching via spaCy's `nlp.pipe()` directly and bypassing Presidio's per-string API — but this requires custom integration beyond Phase 2 scope.

---

## 9. Model Management

### Where the Model Lives

spaCy installs models as Python packages into the sender's virtual environment. On a frozen PyInstaller build, the model would need to be bundled or placed in a known path.

For Phase 2, the sender is **not bundled** with the model. The model is downloaded during onboarding (handled by `luminque-deployment-p2`). The default spaCy install path applies:

```
# Typically within the Python environment:
<venv>\Lib\site-packages\en_core_web_sm\
```

Or, if the sender is a standalone exe, a separate model directory can be configured:

```
%APPDATA%\Luminque\models\en_core_web_sm\
```

For the latter, load the model via absolute path rather than package name:

```python
import spacy
nlp = spacy.load(r"C:\Users\...\AppData\Roaming\Luminque\models\en_core_web_sm")
```

### Verifying the Model Is Present Before Running

The sender must not silently skip scrubbing if the model is absent — this would leak raw PII. Instead, abort the run with a clear error.

```python
# luminque_sender/model.py

import logging
import sys
from pathlib import Path

import spacy

from luminque_sender.constants import SPACY_MODEL_NAME

logger = logging.getLogger(__name__)


def is_model_available() -> bool:
    """Return True if the configured spaCy model is installed."""
    return spacy.util.is_package(SPACY_MODEL_NAME)


def require_spacy_model() -> None:
    """Abort the sender if the spaCy model is not installed.

    This is a hard failure — we will not send unscrubbed data if the
    model is missing. The user must re-run onboarding to install the model.

    Raises:
        SystemExit: Always, with exit code 2, if the model is not found.
    """
    if not is_model_available():
        logger.error(
            f"spaCy model '{SPACY_MODEL_NAME}' is not installed. "
            f"Run luminque onboarding to install it. "
            f"Sender will not transmit unscrubbed data. Exiting."
        )
        sys.exit(2)
    logger.debug(f"spaCy model '{SPACY_MODEL_NAME}' is present.")
```

Exit code `2` is distinct from `1` (send failure) so monitoring can differentiate model-missing errors from network errors.

### Constants

```python
# luminque_sender/constants.py (Phase 2 additions)

SENDER_VERSION = "2.0.0"
PAYLOAD_SCHEMA_VERSION = "2"
SPACY_MODEL_NAME = "en_core_web_sm"
MAX_IMAGES_TO_SCRUB_PER_CYCLE = 50   # Emergency cap; None to disable
```

---

## 10. Changes to Sender Flow vs Phase 1

This section is a delta summary — only what is added or changed is listed.

### sender.py

- After `query_batch`, call `require_spacy_model()`.
- After `require_spacy_model()`, call `scrub_batch()` to get pre-scrubbed dicts.
- Pass scrubbed dicts to `build_payload` instead of raw SQLAlchemy objects.
- `build_payload` signature changes: `action_events: list[dict]` instead of `list[ActionEvent]`, `screenshots: list[dict]` instead of `list[Screenshot]`. Window events are now also passed as scrubbed dicts.

### payload.py

- `build_payload` now accepts pre-scrubbed dicts for events and screenshots. The `serialize_action_event` and `serialize_screenshot` functions are no longer called from the build path (they become dead code and can be removed or left for reference).
- `schema_version` changes from `"1"` to `"2"`.

### constants.py

- `SENDER_VERSION = "2.0.0"`
- `PAYLOAD_SCHEMA_VERSION = "2"`
- `SPACY_MODEL_NAME = "en_core_web_sm"`
- `MAX_IMAGES_TO_SCRUB_PER_CYCLE = 50`

### New modules

- `luminque_sender/capture_loader.py` — `CaptureRecordingLoader`, `scrubbed_action_to_dict`, `scrubbed_screenshot_to_dict`
- `luminque_sender/scrubbing.py` — `scrub_batch`, `get_scrubber`, `scrub_window_event`
- `luminque_sender/window_filter.py` — `is_sensitive_window`, `get_scrub_screenshot_ids`, `_SENSITIVE_PATTERNS`
- `luminque_sender/model.py` — `is_model_available`, `require_spacy_model`

### Updated module structure

```
luminque_sender/
  __init__.py
  __main__.py
  __version__.py
  constants.py         # SPACY_MODEL_NAME, PAYLOAD_SCHEMA_VERSION="2" added
  sender.py            # scrub_batch() call inserted between query and build
  state.py             # Unchanged
  db.py                # Unchanged
  payload.py           # build_payload accepts dicts; schema_version="2"
  transport.py         # Unchanged
  heartbeat.py         # Unchanged
  credentials.py       # Unchanged
  retention.py         # Unchanged
  capture_loader.py    # NEW
  scrubbing.py         # NEW
  window_filter.py     # NEW
  model.py             # NEW
```

---

## 11. Migration Notes: Phase 1 → Phase 2 Rollout

### Deployment Sequence

1. `luminque-deployment-p2` runs on each machine — downloads `en_core_web_sm`, updates the sender exe.
2. First sender run after upgrade picks up `SENDER_VERSION = "2.0.0"` and `schema_version = "2"` in the heartbeat and payload.
3. Server-side ingest must accept both schema version `"1"` and `"2"` simultaneously during the rollout window.

### Server-Side Data Purge

After Phase 2 is fully rolled out to all machines (confirm via heartbeat `sender_version >= 2.0.0` per machine), the server should purge raw Phase 1 data:

- **What to purge:** `key_char`, `canonical_key_char`, `element_state` fields; screenshot blob data; `WindowEvent.state` and `WindowEvent.title` fields — from all payloads received with `schema_version = "1"`.
- **What to retain:** Non-text fields (`mouse_x`, `mouse_y`, `name`, `timestamp`, IDs) can be retained for historical process analysis.
- **Timeline:** Purge 30 days after the last Phase 1 payload is received from any machine. Keep a record of the purge date and scope for compliance purposes.
- **Mechanism:** A one-time server-side migration script. Scope is out of this document — tracked in a separate server migration ticket.

### Mixed-Version Window

During rollout, some machines will send Phase 1 payloads (schema `"1"`) and some will send Phase 2 payloads (schema `"2"`). The server must:

- Accept both schema versions.
- Not backfill scrubbing on Phase 1 data server-side (Phase 1 data is treated as legacy; scrubbing is a client responsibility in Phase 2 and later).
- Track `schema_version` per payload for the purposes of the purge query.

### State File Compatibility

The `sender_state.json` schema is unchanged. No migration of the cursor is required. A machine upgrading from Phase 1 to Phase 2 will simply continue from its existing cursor position — the next batch will be scrubbed before transmission.

---

## 12. Implementation Locations

This section tells a developer exactly where to make changes and what to create. No changes are needed in `openadapt-capture` or `openadapt-privacy` source — both are consumed as read-only dependencies.

### New files to create inside `luminque/sender/`

All four new modules belong at the top level of the `luminque/sender/` package, alongside the Phase 1 files.

| File | Purpose |
|---|---|
| `luminque/sender/capture_loader.py` | `CaptureRecordingLoader`, `scrubbed_action_to_dict`, `scrubbed_screenshot_to_dict` — maps SQLAlchemy capture models to openadapt-privacy dataclasses and back |
| `luminque/sender/scrubbing.py` | `scrub_batch`, `get_scrubber`, `scrub_window_event` — top-level scrubbing entry point; owns the module-level `PresidioScrubbingProvider` singleton |
| `luminque/sender/window_filter.py` | `is_sensitive_window`, `get_scrub_screenshot_ids`, `_SENSITIVE_PATTERNS` — decides which screenshots need image scrubbing based on window title pattern matching |
| `luminque/sender/model.py` | `is_model_available`, `require_spacy_model` — spaCy model presence check; aborts the sender (exit code 2) if the model is missing |

Full implementations for all four files are specified in Sections 3, 4, 6, and 8 respectively.

### Existing files in `luminque/sender/` to modify

**`luminque/sender/__init__.py`**  
Currently a stub (`run()` prints "not implemented yet"). Phase 2 (like Phase 1 before it) will replace this stub with the full sender implementation. The module structure from Section 9 shows the complete target state.

**`luminque/sender/sender.py`** *(created in Phase 1)*  
Insert the scrubbing call after `query_batch` and before `build_payload`. See Section 4 ("Integration Point in sender.py") for the exact delta — three new imports and a `scrub_batch()` call that replaces raw SQLAlchemy objects with pre-scrubbed dicts.

**`luminque/sender/payload.py`** *(created in Phase 1)*  
Update `build_payload` to accept `list[dict]` for `action_events` and `screenshots` instead of SQLAlchemy model lists. Change `schema_version` from `"1"` to `"2"`. See Section 9 for the full delta.

**`luminque/sender/constants.py`** *(created in Phase 1)*  
Add four constants: `SENDER_VERSION = "2.0.0"`, `PAYLOAD_SCHEMA_VERSION = "2"`, `SPACY_MODEL_NAME = "en_core_web_sm"`, `MAX_IMAGES_TO_SCRUB_PER_CYCLE = 50`. See Appendix A for the complete updated file.

### Changes to `pyproject.toml`

`openadapt-privacy` must be added to the `[project]` `dependencies` list in `/Users/aaron_other/Documents/Luminque.nosync/luminque-ops.nosync/pyproject.toml`.

Add it as a git dependency (matching the pattern used for `openadapt-capture`), with the `presidio` extra to pull in Presidio and its spaCy NLP engine:

```toml
[project]
dependencies = [
    "openadapt-capture @ git+https://github.com/luminiq-hq/openadapt-capture",
    "openadapt-privacy[presidio] @ git+https://github.com/OpenAdaptAI/openadapt-privacy",
    "requests>=2.31",
    "psutil>=5.9",
    "keyring>=24.0",
]
```

The `[presidio]` extra installs `presidio-analyzer`, `presidio-anonymizer`, `presidio-image-redactor`, and `spacy` (but not the spaCy model itself — the model is downloaded separately during onboarding via `python -m spacy download en_core_web_sm`).

Also add `Pillow` as an explicit dependency — it is a transitive dep of `openadapt-privacy` but is imported directly in `capture_loader.py`:

```toml
"Pillow>=10.0",
```

### No changes needed in `openadapt-capture` or `openadapt-privacy`

**`openadapt-capture`** (`/Users/aaron_other/Documents/Luminque.nosync/openadapt-capture.nosync/`): read-only. Phase 2 reads its SQLAlchemy models (`ActionEvent`, `Screenshot`, `WindowEvent`) via the existing DB session from `db.py`. No modifications to the fork are required or intended.

**`openadapt-privacy`** (`/Users/aaron_other/Documents/Luminque.nosync/openadapt-privacy.nosync/`): read-only. Installed as a pip dependency. The only interaction with this repo is via its public API (`PresidioScrubbingProvider`, `RecordingLoader`, `Action`, `Screenshot`, `Recording`, `scrub_dict`). The config patch in `scrubbing.py` (Appendix C) mutates the module-level `config` object at runtime — this is not a source change.

---

## 13. Out of Scope for Phase 2

The following are explicitly deferred:

### No Browser Events

`BrowserEvent` rows remain excluded from the payload, as in Phase 1.

### No Audio Scrubbing

`AudioInfo` rows (`flac_data`, `transcribed_text`) are not sent. Audio PII scrubbing requires a separate pipeline and is not addressed here.

### No Adaptive Batch Splitting

Phase 2 does not add payload size splitting. `MAX_BATCH_EVENTS` remains the sole knob for controlling batch size.

### No Transformer Model (en_core_web_trf)

The 500MB transformer model is not shipped or downloaded in Phase 2. Upgrading to `en_core_web_trf` is a Phase 3 consideration, pending performance data from Phase 2 deployments.

### No Pattern Config File

Sensitive window title patterns are hardcoded in `window_filter.py`. A user-configurable or server-pushed pattern list is deferred to Phase 3.

### No Server-Side Scrubbing Fallback

The server does not scrub data on behalf of Phase 2 senders. If the local scrubbing fails and the sender exits with code 2 (model missing), the data is held locally until the model is installed — it is never sent unscrubbed.

### No Log Rotation

Phase 1 noted that `RotatingFileHandler` was deferred. It is still deferred in Phase 2.

### No Windows Event Log Integration

Still deferred, as in Phase 1.

### No Multipart Upload

Screenshots are still embedded as inline base64 in the JSON payload. A multipart or pre-signed URL approach is deferred.

---

## Appendix A: Updated Constants

```python
# luminque_sender/constants.py

SENDER_VERSION = "2.0.0"
MAX_BATCH_EVENTS = 5000
RETENTION_SECONDS = 24 * 60 * 60
REQUEST_TIMEOUT_SECONDS = 60
GZIP_COMPRESS_LEVEL = 6
PAYLOAD_SCHEMA_VERSION = "2"
KEYRING_SERVICE_NAME = "luminque-sender"
DB_FILENAME = "recording.db"
STATE_FILENAME = "sender_state.json"
MACHINE_ID_FILENAME = "machine_id"
LOG_DIR = "logs"
SPACY_MODEL_NAME = "en_core_web_sm"
MAX_IMAGES_TO_SCRUB_PER_CYCLE = 50
```

---

## Appendix B: Key Function Signatures (Phase 2 additions)

```python
# capture_loader.py
class CaptureRecordingLoader(RecordingLoader):
    def from_batch(
        self,
        action_events: list[ActionEvent],
        capture_screenshots: list[CaptureScreenshot],
        window_events: list[WindowEvent],
        scrub_screenshot_ids: set[int],
    ) -> Recording: ...

def scrubbed_action_to_dict(action: Action) -> dict: ...
def scrubbed_screenshot_to_dict(
    privacy_screenshot: PrivacyScreenshot,
    original: CaptureScreenshot,
) -> dict: ...

# scrubbing.py
def scrub_batch(
    action_events: list[ActionEvent],
    capture_screenshots: list[CaptureScreenshot],
    window_events: list[WindowEvent],
    scrubber: PresidioScrubbingProvider,
) -> tuple[list[dict], list[dict]]: ...

def get_scrubber() -> PresidioScrubbingProvider: ...

def scrub_window_event(
    w: WindowEvent,
    scrubber: PresidioScrubbingProvider,
) -> dict: ...

# window_filter.py
def is_sensitive_window(title: Optional[str]) -> tuple[bool, Optional[str]]: ...
def get_scrub_screenshot_ids(
    action_events: list[ActionEvent],
    window_map: dict[int, WindowEvent],
) -> set[int]: ...

# model.py
def is_model_available() -> bool: ...
def require_spacy_model() -> None: ...
```

---

## Appendix C: openadapt-privacy Config for en_core_web_sm

The global `config` object must be patched before the first call to `get_scrubber()` in Phase 2. The cleanest approach is to subclass `PrivacyConfig` and pass it explicitly, but since `openadapt-privacy` uses a module-level `config` singleton, patching is more practical:

```python
# luminque_sender/scrubbing.py — at module top, before any openadapt_privacy import

import openadapt_privacy.config as _privacy_config
_privacy_config.config.SPACY_MODEL_NAME = "en_core_web_sm"
_privacy_config.config.SCRUB_CONFIG_TRF = {
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
}
```

This must happen before any call that touches `_get_analyzer_engine()` in `providers/presidio.py`, which reads `config.SCRUB_CONFIG_TRF` and `config.SPACY_MODEL_NAME` on first initialization.
