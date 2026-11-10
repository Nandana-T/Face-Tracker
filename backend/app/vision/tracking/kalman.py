"""Tiny constant-velocity Kalman filters for box tracking (pure Python, no dependencies).

Four independent 2-state filters (position, velocity) run on the box centre x/y, width and height.
Time enters explicitly (``dt`` seconds), so a detection every 120 ms, every 400 ms or at irregular
intervals all give consistent velocity estimates - nothing assumes a fixed frame rate.
Noise is expressed relative to the box size so small and large faces behave alike.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Axis:
    """State [p, v] with covariance [[p00, p01], [p01, p11]]."""

    p: float
    v: float
    p00: float
    p01: float
    p11: float

    def predict(self, dt: float, accel_sigma: float, velocity_decay: float = 1.0) -> None:
        if dt <= 0:
            return
        if velocity_decay != 1.0:
            self.v *= velocity_decay
        self.p += self.v * dt
        # F P F^T with F = [[1, dt], [0, 1]]
        p00 = self.p00 + dt * (2 * self.p01 + dt * self.p11)
        p01 = self.p01 + dt * self.p11
        p11 = self.p11
        q = accel_sigma * accel_sigma
        self.p00 = p00 + q * dt**4 / 4
        self.p01 = p01 + q * dt**3 / 2
        self.p11 = p11 + q * dt**2

    def update(self, z: float, meas_sigma: float) -> None:
        r = meas_sigma * meas_sigma
        s = self.p00 + r
        k0, k1 = self.p00 / s, self.p01 / s
        innovation = z - self.p
        self.p += k0 * innovation
        self.v += k1 * innovation
        p00, p01, p11 = self.p00, self.p01, self.p11
        self.p00 = (1 - k0) * p00
        self.p01 = (1 - k0) * p01
        self.p11 = p11 - k1 * p01


class BoxFilter:
    """Filters ``(cx, cy, w, h)``; `box` returns the current estimate as ``x1, y1, x2, y2``."""

    def __init__(
        self,
        box: tuple[float, float, float, float],
        accel_scale: float = 4.0,
        meas_scale: float = 0.06,
    ) -> None:
        x1, y1, x2, y2 = box
        w, h = x2 - x1, y2 - y1
        self._accel_scale = accel_scale
        self._meas_scale = meas_scale
        size = max(w, h)
        pos_var = (meas_scale * size) ** 2
        vel_var = (1.0 * size) ** 2  # up to ~one box size per second is unsurprising
        self.axes = [
            Axis((x1 + x2) / 2, 0.0, pos_var, 0.0, vel_var),
            Axis((y1 + y2) / 2, 0.0, pos_var, 0.0, vel_var),
            Axis(w, 0.0, pos_var, 0.0, vel_var),
            Axis(h, 0.0, pos_var, 0.0, vel_var),
        ]

    def _size(self) -> float:
        return max(self.axes[2].p, self.axes[3].p, 1.0)

    def predict(self, dt: float, velocity_decay_per_second: float = 0.0) -> None:
        """Advance by ``dt`` seconds. ``velocity_decay_per_second`` > 0 damps the velocity
        (used for LOST tracks, whose last velocity becomes less trustworthy with time)."""
        if dt <= 0:
            return
        decay = math.exp(-velocity_decay_per_second * dt) if velocity_decay_per_second else 1.0
        size = self._size()
        for index, axis in enumerate(self.axes):
            sigma = self._accel_scale * size if index < 2 else 1.0 * size
            axis.predict(dt, sigma, decay)
        # Sizes must stay positive.
        for axis in self.axes[2:]:
            axis.p = max(axis.p, 1.0)

    def update(self, box: tuple[float, float, float, float]) -> None:
        x1, y1, x2, y2 = box
        meas = (
            (x1 + x2) / 2,
            (y1 + y2) / 2,
            max(x2 - x1, 1.0),
            max(y2 - y1, 1.0),
        )
        sigma = self._meas_scale * self._size()
        for axis, z in zip(self.axes, meas, strict=True):
            axis.update(z, sigma)

    def box(self) -> tuple[float, float, float, float]:
        cx, cy, w, h = (a.p for a in self.axes)
        return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
