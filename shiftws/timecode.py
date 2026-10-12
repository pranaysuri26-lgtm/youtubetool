"""Rational frame rates and frame positions. Frames (integers) are canonical; seconds are derived for display.

29.97 is 30000/1001 and 59.94 is 60000/1001 exactly — never 29.97 as a float.
Premiere uses 254016000000 ticks per second; conversions are exact for all broadcast rates.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

PREMIERE_TICKS_PER_SECOND = 254_016_000_000

_COMMON = {
    "23.976": Fraction(24000, 1001), "24": Fraction(24), "25": Fraction(25),
    "29.97": Fraction(30000, 1001), "30": Fraction(30), "50": Fraction(50),
    "59.94": Fraction(60000, 1001), "60": Fraction(60),
}


@dataclass(frozen=True)
class Rate:
    num: int
    den: int

    @property
    def frac(self) -> Fraction:
        return Fraction(self.num, self.den)

    @classmethod
    def parse(cls, s: str | float | Fraction) -> "Rate":
        if isinstance(s, Fraction):
            f = s
        elif isinstance(s, str) and "/" in s:
            n, d = s.split("/")
            f = Fraction(int(n), int(d)) if int(d) else Fraction(0)
        else:
            key = str(s)
            f = _COMMON.get(key) or snap(Fraction(str(s)))
        return cls(f.numerator, f.denominator)

    def __str__(self) -> str:
        return f"{self.num}/{self.den}"

    def to_dict(self) -> dict:
        return {"num": self.num, "den": self.den}

    # conversions
    def frames_to_seconds(self, frames: int) -> float:
        return float(Fraction(frames) / self.frac)

    def seconds_to_frames(self, seconds: float, rounding: str = "nearest") -> int:
        exact = Fraction(str(seconds)) * self.frac
        if rounding == "floor":
            return exact.numerator // exact.denominator
        if rounding == "ceil":
            return -((-exact.numerator) // exact.denominator)
        return round(exact)

    def frames_to_ticks(self, frames: int) -> int:
        t = Fraction(frames) * PREMIERE_TICKS_PER_SECOND / self.frac
        return round(t)

    def timecode(self, frames: int) -> str:
        """Non-drop-frame display timecode (HH:MM:SS:FF) using the nominal integer rate."""
        nominal = round(self.frac)
        ff = frames % nominal
        s = frames // nominal
        return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{ff:02d}"


def snap(f: Fraction) -> Fraction:
    """Snap a measured rate (e.g. 29.97002997) to the nearest standard rational within 0.05%."""
    for std in _COMMON.values():
        if abs(float(f - std)) / float(std) < 0.0005:
            return std
    return f.limit_denominator(1001)


def rescale(frames: int, src: Rate, dst: Rate) -> int:
    """Map a frame index in one rate to the nearest frame in another."""
    return round(Fraction(frames) * dst.frac / src.frac)
