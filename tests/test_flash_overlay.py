"""Unit tests for the screenshot flash overlay.

The actual Tk window is never created in these tests — they exercise the
public dispatch surface (env-var gating, lifecycle bookkeeping, fallthrough
when ``tkinter`` cannot be imported).
"""

import sys
import threading
from unittest.mock import patch

import pytest

import windows_mcp.desktop.flash_overlay as flash_overlay


@pytest.fixture(autouse=True)
def _reset_active_overlay():
    """Each test starts and ends with no overlay registered."""
    with flash_overlay._lock:
        flash_overlay._active_overlay = None
    yield
    with flash_overlay._lock:
        ov = flash_overlay._active_overlay
        flash_overlay._active_overlay = None
    if ov is not None:
        ov.stop_event.set()


class TestFlashDisabled:
    def test_default_is_enabled(self, monkeypatch):
        monkeypatch.delenv("WINDOWS_MCP_DISABLE_FLASH", raising=False)
        assert flash_overlay._flash_disabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " On "])
    def test_truthy_values_disable(self, monkeypatch, value):
        monkeypatch.setenv("WINDOWS_MCP_DISABLE_FLASH", value)
        assert flash_overlay._flash_disabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_falsy_values_keep_enabled(self, monkeypatch, value):
        monkeypatch.setenv("WINDOWS_MCP_DISABLE_FLASH", value)
        assert flash_overlay._flash_disabled() is False


class TestShowCaptureFlash:
    def test_disabled_env_var_skips_thread(self, monkeypatch):
        monkeypatch.setenv("WINDOWS_MCP_DISABLE_FLASH", "1")
        with patch.object(threading, "Thread") as fake_thread:
            flash_overlay.show_capture_flash([(0, 0, 100, 100)], full_screen=False)
        fake_thread.assert_not_called()
        assert flash_overlay._active_overlay is None

    def test_empty_rects_skips_thread(self, monkeypatch):
        monkeypatch.delenv("WINDOWS_MCP_DISABLE_FLASH", raising=False)
        with patch.object(threading, "Thread") as fake_thread:
            flash_overlay.show_capture_flash([], full_screen=False)
        fake_thread.assert_not_called()
        assert flash_overlay._active_overlay is None

    def test_registers_overlay_and_starts_daemon_thread(self, monkeypatch):
        monkeypatch.delenv("WINDOWS_MCP_DISABLE_FLASH", raising=False)

        captured = {}

        class _StubThread:
            def __init__(self, target, args, name, daemon):
                captured["target"] = target
                captured["args"] = args
                captured["name"] = name
                captured["daemon"] = daemon

            def start(self):
                captured["started"] = True

        monkeypatch.setattr(flash_overlay.threading, "Thread", _StubThread)

        flash_overlay.show_capture_flash([(10, 20, 110, 120)], full_screen=True)

        assert captured["started"] is True
        assert captured["daemon"] is True
        assert captured["name"] == "windows-mcp-flash"
        assert flash_overlay._active_overlay is not None
        # rects passed through; full_screen flag forwarded
        rects_arg, full_screen_arg, overlay_arg = captured["args"]
        assert rects_arg == [(10, 20, 110, 120)]
        assert full_screen_arg is True
        assert overlay_arg is flash_overlay._active_overlay


class TestCancelActiveFlash:
    def test_no_op_when_no_active_overlay(self):
        flash_overlay.cancel_active_flash()
        assert flash_overlay._active_overlay is None

    def test_signals_stop_and_clears_active(self, monkeypatch):
        # Install a stub overlay manually so we don't depend on Tk
        overlay = flash_overlay._Overlay()
        overlay.thread = threading.Thread(target=lambda: None, daemon=True)
        overlay.thread.start()
        overlay.thread.join()
        with flash_overlay._lock:
            flash_overlay._active_overlay = overlay

        flash_overlay.cancel_active_flash(timeout=0.1)

        assert overlay.stop_event.is_set()
        assert flash_overlay._active_overlay is None


class TestLayerColor:
    def test_full_intensity_returns_orange_red(self):
        assert flash_overlay._layer_color(1.0).upper() == "#FF4500"

    def test_half_intensity_blends_toward_black(self):
        c = flash_overlay._layer_color(0.5)
        # 0xFF * 0.5 = 127 -> 7F; 0x45 * 0.5 = 34 -> 22; 0x00 * 0.5 = 0 -> 00.
        assert c.upper() == "#7F2200"

    def test_zero_intensity_avoids_pure_black(self):
        # Pure black is the transparent-colour key; the helper must not return it
        # because a 0-intensity layer would otherwise punch a hole in the canvas.
        c = flash_overlay._layer_color(0.0)
        assert c.upper() != "#000000"
        assert c.upper() == "#010000"


class TestRunOverlayFallthrough:
    def test_missing_tkinter_sets_closed_event(self, monkeypatch):
        # Force ``import tkinter`` inside _run_overlay to fail
        monkeypatch.setitem(sys.modules, "tkinter", None)
        overlay = flash_overlay._Overlay()
        flash_overlay._run_overlay([(0, 0, 100, 100)], False, overlay)
        assert overlay.closed_event.is_set()
