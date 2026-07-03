"""User-activity detection via pynput hooks — storage-free.

The listener callbacks do exactly one thing: record the time of the last
input event. No event objects are queued or persisted; this is also the
insertion point for future action-event capture (design doc §9).

If the hooks cannot start (endpoint security blocking hook injection, missing
OS permissions), the monitor degrades to "always active": the capture loop
then samples continuously and relies on dedupe to suppress unchanged frames.
"""

import logging
import time

logger = logging.getLogger(__name__)


class ActivityMonitor:
    def __init__(self) -> None:
        self._last_activity = time.monotonic()
        self._listeners: list = []
        self._start_failed = False

    def start(self) -> None:
        started: list = []
        try:
            from pynput import keyboard, mouse

            keyboard_listener = keyboard.Listener(
                on_press=self._on_event, on_release=self._on_event
            )
            keyboard_listener.start()
            started.append(keyboard_listener)

            mouse_listener = mouse.Listener(
                on_move=self._on_event,
                on_click=self._on_event,
                on_scroll=self._on_event,
            )
            mouse_listener.start()
            started.append(mouse_listener)

            self._listeners = started
            logger.info("Input listeners started")
        except Exception:
            logger.exception(
                "Input listeners failed to start — degrading to continuous sampling"
            )
            # Don't orphan a hook thread that started before the failure.
            for listener in started:
                try:
                    listener.stop()
                except Exception:
                    pass
            self._start_failed = True

    def stop(self) -> None:
        for listener in self._listeners:
            try:
                listener.stop()
            except Exception:
                pass
        self._listeners = []

    def _on_event(self, *args, **kwargs) -> None:
        self._last_activity = time.monotonic()

    @property
    def degraded(self) -> bool:
        """True when activity cannot be observed and the loop must sample continuously."""
        if self._start_failed:
            return True
        # Listener threads can die after a successful start (e.g. permission
        # revoked, hook unloaded). If none survive, activity is unobservable.
        if self._listeners and not any(l.is_alive() for l in self._listeners):
            return True
        return False

    def active_within(self, seconds: float) -> bool:
        if self.degraded:
            return True
        return (time.monotonic() - self._last_activity) <= seconds
