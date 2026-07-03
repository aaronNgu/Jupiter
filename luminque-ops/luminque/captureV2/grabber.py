"""Screen grabbing and image processing for captureV2.

mss for capture (primary monitor only — Phase 1 scope), PIL for
downscale/encode. The dhash/hamming pair is the change detector: ~15 lines
of stdlib+PIL instead of an imagehash dependency.
"""

import io
import logging
import sys
import threading
import time

from PIL import Image, ImageStat

logger = logging.getLogger(__name__)

_GEOMETRY_CHECK_INTERVAL_SECONDS = 60.0


def _disable_captureblt() -> None:
    """Turn off CAPTUREBLT in BitBlt calls (mouse-cursor flicker, slower grabs).

    mss >= 10 moved the flag from the mss.windows module to the
    mss.windows.gdi submodule, where it is read as a module global on every
    grab. Patch whichever location exists; never fail the grab over it.
    """
    try:
        from mss.windows import gdi

        gdi.CAPTUREBLT = 0
        return
    except ImportError:
        pass
    try:
        import mss.windows

        mss.windows.CAPTUREBLT = 0  # mss < 10
    except Exception:
        logger.warning("Could not disable CAPTUREBLT", exc_info=True)


class Grabber:
    """Wraps an mss instance; recreates it after a failed grab.

    A grab can fail transiently (session lock, display change, RDP
    disconnect). The mss handle may be stale afterwards, so it is dropped on
    failure and lazily recreated on the next call.
    """

    def __init__(self) -> None:
        self._local = threading.local()
        self._next_geometry_check = 0.0

    def _sct(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            import mss

            if sys.platform == "win32":
                # https://github.com/BoboTiG/python-mss/issues/179
                _disable_captureblt()
            sct = mss.mss()
            self._local.sct = sct
        return sct

    def reset(self) -> None:
        """Drop the mss handle; the next grab re-reads monitor geometry."""
        sct = getattr(self._local, "sct", None)
        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass
        self._local.sct = None

    def _geometry_stale(self, monitor: dict) -> bool:
        """mss caches monitor geometry per instance. After a resolution change
        (RDP reconnect, dock/undock) BitBlt against the stale rect often still
        succeeds but returns cropped or black frames — so exceptions alone are
        not enough to trigger a reset. Compare against live metrics, at most
        once per _GEOMETRY_CHECK_INTERVAL_SECONDS to bound cost and log spam.
        """
        if sys.platform != "win32":
            return False
        now = time.monotonic()
        if now < self._next_geometry_check:
            return False
        self._next_geometry_check = now + _GEOMETRY_CHECK_INTERVAL_SECONDS
        try:
            import ctypes

            user32 = ctypes.windll.user32
            live = (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
            return live != (monitor["width"], monitor["height"])
        except Exception:
            return False

    def monitor_size(self) -> tuple[int, int] | None:
        """(width, height) of the primary monitor, or None if unavailable."""
        try:
            mon = self._sct().monitors[1]
            return mon["width"], mon["height"]
        except Exception:
            logger.warning("Could not read monitor size", exc_info=True)
            self.reset()
            return None

    def grab(self) -> Image.Image | None:
        """Grab the primary monitor as an RGB PIL image, or None on failure."""
        try:
            sct = self._sct()
            monitor = sct.monitors[1]
            if self._geometry_stale(monitor):
                logger.info("Display geometry changed — reinitializing grabber")
                self.reset()
                sct = self._sct()
                monitor = sct.monitors[1]
            raw = sct.grab(monitor)
            return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        except Exception:
            logger.warning("Screen grab failed", exc_info=True)
            self.reset()
            return None


def downscale(image: Image.Image, max_width: int) -> Image.Image:
    if image.width <= max_width:
        return image
    height = max(1, round(image.height * max_width / image.width))
    return image.resize((max_width, height), Image.BILINEAR)


def to_thumb(image: Image.Image, width: int) -> Image.Image:
    """Small grayscale thumbnail used for brightness and hashing."""
    height = max(1, round(image.height * width / image.width))
    return image.convert("L").resize((width, height), Image.BILINEAR)


def brightness(gray_image: Image.Image) -> float:
    return ImageStat.Stat(gray_image).mean[0]


def dhash(gray_image: Image.Image, hash_size: int = 8) -> int:
    """Difference hash: compares horizontally adjacent pixels of a small grid."""
    img = gray_image.resize((hash_size + 1, hash_size), Image.BILINEAR)
    px = img.tobytes()  # mode "L": one byte per pixel, row-major
    bits = 0
    row_width = hash_size + 1
    for row in range(hash_size):
        for col in range(hash_size):
            left = px[row * row_width + col]
            right = px[row * row_width + col + 1]
            bits = (bits << 1) | (left > right)
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def encode_png(image: Image.Image, compress_level: int) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG", compress_level=compress_level)
    return buf.getvalue()
