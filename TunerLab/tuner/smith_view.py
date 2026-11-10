"""Shared zoom/pan view transform for every Smith-chart widget (F-24).

Both chart families use this module so a wheel gesture, a middle-button drag
and a double click behave identically across the app:

* :class:`tuner.ui.SmithChart`  — dark measured-point chart (Smith page,
  Pattern Editor, legacy mock window).
* :class:`tunerlab_app.ui.SmithPreview` — light wizard point-plan preview.

The transform is deliberately pure Python (no Qt types) so the geometry can be
regression-tested without an event loop.  It maps a reflection coefficient to a
pixel with an optional magnification and a pixel-space pan::

    xy(g) = (cx + pan_x + R*zoom*Re(g), cy + pan_y - R*zoom*Im(g))

``zoom == 1`` and zero pan reproduce the original unzoomed chart exactly, so
click-to-pick and every existing geometry test keep their original behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ['SmithView']


@dataclass
class SmithView:
    """Mutable view state; the owning widget calls :meth:`to_pixel` on paint."""

    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    min_zoom: float = 1.0
    max_zoom: float = 60.0

    # -- state -----------------------------------------------------------
    def reset(self) -> None:
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0

    @property
    def zoomed(self) -> bool:
        return abs(self.zoom - 1.0) > 1e-9 or abs(self.pan_x) > 1e-9 or abs(self.pan_y) > 1e-9

    # -- geometry --------------------------------------------------------
    @staticmethod
    def base_radius(width: float, height: float, inset: float) -> float:
        return max(1.0, (min(width, height) - inset) / 2.0)

    def radius(self, width: float, height: float, inset: float) -> float:
        return self.base_radius(width, height, inset) * self.zoom

    def to_pixel(self, gamma, width: float, height: float, inset: float):
        """Return ``(x, y)`` widget pixels for a reflection coefficient."""
        r = self.radius(width, height, inset)
        cx, cy = width / 2.0, height / 2.0
        return (cx + self.pan_x + r * gamma.real,
                cy + self.pan_y - r * gamma.imag)

    def to_gamma(self, x: float, y: float, width: float, height: float,
                 inset: float):
        """Inverse of :meth:`to_pixel`; used for click-to-pick."""
        r = self.radius(width, height, inset)
        cx, cy = width / 2.0, height / 2.0
        return complex((x - cx - self.pan_x) / r,
                       (cy + self.pan_y - y) / r)

    # -- interaction -----------------------------------------------------
    def zoom_at(self, factor: float, x: float, y: float, width: float,
                height: float, inset: float) -> bool:
        """Zoom around the pixel ``(x, y)``, keeping the Γ under it pinned.

        Returns ``True`` when the view actually changed (so the caller can
        schedule a repaint).  ``factor > 1`` magnifies.
        """
        old = self.zoom
        new = min(self.max_zoom, max(self.min_zoom, old * factor))
        if abs(new - old) < 1e-12:
            return False
        anchor = self.to_gamma(x, y, width, height, inset)  # under the OLD zoom
        self.zoom = new
        r = self.radius(width, height, inset)
        cx, cy = width / 2.0, height / 2.0
        self.pan_x = x - cx - r * anchor.real
        self.pan_y = y - cy + r * anchor.imag
        self.clamp(width, height, inset)
        return True

    def zoom_step(self, direction: int, width: float, height: float,
                  inset: float) -> bool:
        """Zoom about the chart centre (used by the +/− buttons)."""
        factor = 1.3 if direction > 0 else 1.0 / 1.3
        return self.zoom_at(factor, width / 2.0, height / 2.0,
                            width, height, inset)

    def pan_by(self, dx: float, dy: float) -> None:
        self.pan_x += dx
        self.pan_y += dy

    def zoom_to_rect(self, x1: float, y1: float, x2: float, y2: float,
                     width: float, height: float, inset: float,
                     margin: float = 0.92) -> bool:
        """Fit the screen rectangle ``(x1,y1)-(x2,y2)`` into the chart.

        Rubber-band zoom: the two corners are inverted through the *current*
        transform, then the view is recentred on the chosen Γ box and scaled so
        it fills the chart (leaving ``margin`` breathing room).  Returns ``True``
        when the view actually changed.
        """
        if abs(x2 - x1) < 4 or abs(y2 - y1) < 4:
            return False
        g1 = self.to_gamma(x1, y1, width, height, inset)
        g2 = self.to_gamma(x2, y2, width, height, inset)
        re_lo, re_hi = sorted((g1.real, g2.real))
        im_lo, im_hi = sorted((g1.imag, g2.imag))
        half_re = max((re_hi - re_lo) / 2.0, 1e-4)
        half_im = max((im_hi - im_lo) / 2.0, 1e-4)
        center = complex((re_lo + re_hi) / 2.0, (im_lo + im_hi) / 2.0)
        base = self.base_radius(width, height, inset)
        # Scale so the chosen Γ box (half extents) fills the available extent.
        new_zoom = min(base / half_re, base / half_im) * margin
        new_zoom = min(self.max_zoom, max(self.min_zoom, new_zoom))
        self.zoom = new_zoom
        r = self.radius(width, height, inset)
        cx, cy = width / 2.0, height / 2.0
        self.pan_x = -r * center.real
        self.pan_y = r * center.imag
        self.clamp(width, height, inset)
        return True

    def clamp(self, width: float, height: float, inset: float) -> None:
        """Keep at least part of the unit disk on screen at high zoom."""
        r = self.radius(width, height, inset)
        limit = r + max(width, height) * 0.25
        self.pan_x = max(-limit, min(limit, self.pan_x))
        self.pan_y = max(-limit, min(limit, self.pan_y))
