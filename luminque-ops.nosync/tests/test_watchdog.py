"""
Tests for luminque.watchdog.
"""
from datetime import datetime
from unittest.mock import MagicMock, patch


class TestFindCaptureProcess:
    def test_returns_none_when_no_match(self):
        from luminque.watchdog import _find_capture_process

        mock_proc = MagicMock()
        mock_proc.info = {"pid": 123, "exe": "python.exe", "cmdline": ["python", "other.py"]}

        with patch("psutil.process_iter", return_value=[mock_proc]):
            result = _find_capture_process()
        assert result is None

    def test_returns_proc_when_luminque_exe_with_capture_flag(self):
        from luminque.watchdog import _find_capture_process

        mock_proc = MagicMock()
        mock_proc.info = {
            "pid": 456,
            "exe": r"C:\Users\foo\luminque.exe",
            "cmdline": [r"C:\Users\foo\luminque.exe", "--capture"],
        }

        with patch("psutil.process_iter", return_value=[mock_proc]):
            result = _find_capture_process()
        assert result is mock_proc

    def test_returns_proc_by_cmdline(self):
        from luminque.watchdog import _find_capture_process

        mock_proc = MagicMock()
        mock_proc.info = {
            "pid": 789,
            "exe": r"C:\Programs\Luminque\luminque.exe",
            "cmdline": [r"C:\Programs\Luminque\luminque.exe", "--capture"],
        }

        with patch("psutil.process_iter", return_value=[mock_proc]):
            result = _find_capture_process()
        assert result is mock_proc


class TestMidnightWindow:
    def test_true_in_midnight_window(self):
        from luminque.watchdog import _is_midnight_window

        with patch("luminque.watchdog.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 0, 3, 0)
            assert _is_midnight_window() is True

    def test_false_outside_midnight_window(self):
        from luminque.watchdog import _is_midnight_window

        with patch("luminque.watchdog.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 10, 30, 0)
            assert _is_midnight_window() is False

    def test_false_at_midnight_plus_five(self):
        from luminque.watchdog import _is_midnight_window

        with patch("luminque.watchdog.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 0, 6, 0)
            assert _is_midnight_window() is False


class TestWatchdogRun:
    def test_starts_capture_when_not_running(self):
        from luminque.watchdog import run

        with patch("luminque.watchdog._find_capture_process", return_value=None), \
             patch("luminque.watchdog._start_capture") as mock_start:
            run()
        mock_start.assert_called_once()

    def test_restarts_on_rss_exceeded(self):
        from luminque.watchdog import RSS_LIMIT_BYTES, run

        mock_proc = MagicMock()
        mock_proc.memory_info.return_value.rss = RSS_LIMIT_BYTES + 1

        with patch("luminque.watchdog._find_capture_process", return_value=mock_proc), \
             patch("luminque.watchdog._start_capture") as mock_start, \
             patch("luminque.watchdog._is_midnight_window", return_value=False):
            run()
        mock_proc.terminate.assert_called_once()
        mock_start.assert_called_once()

    def test_restarts_on_midnight(self):
        from luminque.watchdog import RSS_LIMIT_BYTES, run

        mock_proc = MagicMock()
        mock_proc.memory_info.return_value.rss = 100 * 1024 * 1024  # 100MB, under limit

        with patch("luminque.watchdog._find_capture_process", return_value=mock_proc), \
             patch("luminque.watchdog._start_capture") as mock_start, \
             patch("luminque.watchdog._is_midnight_window", return_value=True):
            run()
        mock_proc.terminate.assert_called_once()
        mock_start.assert_called_once()

    def test_no_restart_when_healthy(self):
        from luminque.watchdog import RSS_LIMIT_BYTES, run

        mock_proc = MagicMock()
        mock_proc.memory_info.return_value.rss = 100 * 1024 * 1024  # 100MB

        with patch("luminque.watchdog._find_capture_process", return_value=mock_proc), \
             patch("luminque.watchdog._start_capture") as mock_start, \
             patch("luminque.watchdog._is_midnight_window", return_value=False):
            run()
        mock_proc.terminate.assert_not_called()
        mock_start.assert_not_called()
