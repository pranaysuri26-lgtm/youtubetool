"""Fast unit tests for precision-critical pieces (no media needed)."""
from fractions import Fraction

from shiftws.timecode import PREMIERE_TICKS_PER_SECOND, Rate, rescale, snap


def test_rational_rates_are_exact():
    r = Rate.parse("29.97")
    assert (r.num, r.den) == (30000, 1001)
    assert Rate.parse("59.94").frac == Fraction(60000, 1001)
    assert Rate.parse("30000/1001") == r
    assert snap(Fraction(2997003, 100000)) == Fraction(30000, 1001)      # measured 29.97003 → exact NTSC


def test_frames_seconds_ticks_roundtrip():
    r = Rate(30000, 1001)
    # one hour of NTSC frames is 107892 frames + 0.4 → exact arithmetic, no float drift
    assert r.seconds_to_frames(3600, "floor") == 107892
    assert r.frames_to_ticks(30000) == 1001 * PREMIERE_TICKS_PER_SECOND
    assert rescale(1001, Rate(30000, 1001), Rate(60000, 1001)) == 2002
    assert Rate(24, 1).timecode(24 * 61 + 5) == "00:01:01:05"


def test_tagged_and_clean_stay_synchronized():
    from shiftws.scripts_eleven import TAG_RE, strip_tags, words
    t = "[curious] The first room looks safe. [whispers] It is not."
    assert strip_tags(t) == "The first room looks safe. It is not."
    assert words(strip_tags(t)) == words(TAG_RE.sub(" ", t))


def test_time_window_hints():
    from shiftws.chat.engine import time_window
    assert time_window("analyze the first 60 seconds") == (0.0, 60.0)
    assert time_window("from 1:20 to 2:05") == (80.0, 125.0)
    assert time_window("just analyze it") == (None, None)


def test_redaction_and_fts_query():
    from shiftws.util import redact
    from shiftws.workspace import _fts_query
    assert "abc123secret" not in redact("cookie=abc123secret token: abc123secret")
    assert "sk-" not in redact("key sk-ABCDEFGHIJKLMNOP1234")
    assert _fts_query("Find the impact we approved for my AC Short") == '"impact"* OR "approved"* OR "ac"* OR "short"*'
