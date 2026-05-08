"""Brief on-screen visual confirmation that a screenshot was taken.

Renders a glowing orange-red halo around the captured area for ~2.5 s on a
single transparent always-on-top Tk window. The "glow" is produced by
drawing concentric border rectangles whose RGB is blended from full
orange-red toward pure black; the canvas's transparent-colour key is set to
pure black, so pixels that fade to black become genuinely transparent. The
time fade is achieved by re-rendering with a scaled intensity each frame —
``-alpha`` is deliberately avoided because it combines unreliably with
``-transparentcolor`` on Windows.

The flash is started *after* capture and any active overlay is torn down
before the next capture, so it never appears in a captured image.
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_FLASH_RGB = (0xFF, 0x45, 0x00)
_TRANSPARENT_COLOR = "#000000"
_DURATION_MS = 2500
_FRAME_INTERVAL_MS = 20
_GLOW_LAYERS = 12
_FULLSCREEN_INSET = 4
_MIN_VISIBLE_INTENSITY = 0.04

_lock = threading.Lock()
_active_overlay: "_Overlay | None" = None


def _flash_disabled() -> bool:
    value = os.getenv("WINDOWS_MCP_DISABLE_FLASH", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


class _Overlay:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.closed_event = threading.Event()
        self.thread: threading.Thread | None = None


def cancel_active_flash(timeout: float = 0.25) -> None:
    """Tear down any flash overlay currently on screen."""
    global _active_overlay
    with _lock:
        ov = _active_overlay
        _active_overlay = None
    if ov is None:
        return
    ov.stop_event.set()
    ov.closed_event.wait(timeout=timeout)


def show_capture_flash(
    rects: list[tuple[int, int, int, int]],
    *,
    full_screen: bool,
) -> None:
    """Show a fade-in/out orange-red glow around each rect.

    ``rects`` are ``(left, top, right, bottom)`` tuples in virtual-screen
    coordinates. ``full_screen=True`` draws an inner glow that radiates
    inward from each monitor edge. ``full_screen=False`` draws an outer
    halo around the captured region. Returns immediately; rendering happens
    on a daemon thread.
    """
    if _flash_disabled() or not rects:
        return
    rects = [tuple(r) for r in rects]
    overlay = _Overlay()
    overlay.thread = threading.Thread(
        target=_run_overlay,
        args=(rects, full_screen, overlay),
        name="windows-mcp-flash",
        daemon=True,
    )
    with _lock:
        global _active_overlay
        _active_overlay = overlay
    overlay.thread.start()


def _layer_color(intensity: float) -> str:
    """Return a Tk hex colour string for orange-red scaled by ``intensity``."""
    r = int(_FLASH_RGB[0] * intensity)
    g = int(_FLASH_RGB[1] * intensity)
    b = int(_FLASH_RGB[2] * intensity)
    # Avoid landing exactly on the transparent-colour key for very-dim layers.
    if r + g + b == 0:
        r = 1
    return f"#{r:02X}{g:02X}{b:02X}"


def _run_overlay(
    rects: list[tuple[int, int, int, int]],
    full_screen: bool,
    overlay: _Overlay,
) -> None:
    try:
        import tkinter as tk
    except Exception:
        logger.debug("tkinter unavailable; skipping screenshot flash")
        overlay.closed_event.set()
        return

    root: "tk.Tk | None" = None
    try:
        union_left = min(r[0] for r in rects)
        union_top = min(r[1] for r in rects)
        union_right = max(r[2] for r in rects)
        union_bottom = max(r[3] for r in rects)
        # Region mode expands outward by up to _GLOW_LAYERS pixels; widen the
        # window so the outer layers stay inside the canvas.
        if not full_screen:
            union_left -= _GLOW_LAYERS
            union_top -= _GLOW_LAYERS
            union_right += _GLOW_LAYERS
            union_bottom += _GLOW_LAYERS
        width = union_right - union_left
        height = union_bottom - union_top
        if width <= 0 or height <= 0:
            return

        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=_TRANSPARENT_COLOR)
        try:
            root.attributes("-transparentcolor", _TRANSPARENT_COLOR)
        except tk.TclError:
            pass
        root.geometry(f"{width}x{height}+{union_left}+{union_top}")

        canvas = tk.Canvas(
            root,
            width=width,
            height=height,
            bg=_TRANSPARENT_COLOR,
            highlightthickness=0,
            borderwidth=0,
        )
        canvas.pack(fill="both", expand=True)

        base_inset = _FULLSCREEN_INSET if full_screen else 0

        def render(time_alpha: float) -> None:
            canvas.delete("glow")
            for layer in range(_GLOW_LAYERS):
                falloff = (1.0 - layer / _GLOW_LAYERS) ** 2
                intensity = falloff * time_alpha
                if intensity < _MIN_VISIBLE_INTENSITY:
                    continue
                color = _layer_color(intensity)
                offset = base_inset + layer if full_screen else -layer
                for r_left, r_top, r_right, r_bottom in rects:
                    x1 = r_left - union_left + offset
                    y1 = r_top - union_top + offset
                    x2 = r_right - union_left - offset - 1
                    y2 = r_bottom - union_top - offset - 1
                    if x2 - x1 <= 0 or y2 - y1 <= 0:
                        continue
                    canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=1, tags="glow")

        start = time.perf_counter()

        def tick() -> None:
            if overlay.stop_event.is_set():
                root.destroy()
                return
            elapsed_ms = (time.perf_counter() - start) * 1000
            if elapsed_ms >= _DURATION_MS:
                root.destroy()
                return
            t_norm = elapsed_ms / _DURATION_MS
            if full_screen:
                time_alpha = 1.0 - abs(2 * t_norm - 1)
            elif t_norm < 0.15:
                time_alpha = t_norm / 0.15
            elif t_norm < 0.65:
                time_alpha = 1.0
            else:
                time_alpha = max(0.0, 1.0 - (t_norm - 0.65) / 0.35)
            render(time_alpha)
            root.after(_FRAME_INTERVAL_MS, tick)

        render(1.0)
        root.after(0, tick)
        root.mainloop()
    except Exception:
        logger.debug("screenshot flash overlay failed", exc_info=True)
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
    finally:
        with _lock:
            global _active_overlay
            if _active_overlay is overlay:
                _active_overlay = None
        overlay.closed_event.set()
