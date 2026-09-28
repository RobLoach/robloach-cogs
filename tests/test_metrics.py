"""The press timings, and the event-loop lag sampler.

`retro/metrics.py` exists to answer a question production asked once and
nothing could answer: a click that Discord sent more than three seconds ago
cannot be acknowledged (`10062`), and telling a slow bot from a slow network
needs the bot measured. These hold the cheap half of that -- the arithmetic and
the reporting -- against the promises the module makes about never raising.
"""

import asyncio
import logging

import pytest

from .loader import load_standalone

# Loaded standalone, not `from retro import metrics`: retro/__init__.py imports
# Red, which imports discord.py, and the buildbot CI job installs neither. The
# module itself needs nothing but the standard library, so it is one of the
# few that can be tested with no dependencies at all -- see tests/README.md.
M = load_standalone("retro_metrics_standalone", "metrics.py")


def test_one_recording_is_its_own_mean_and_worst():
    m = M.Metrics()
    m.record("encode", 0.25)
    stat = m.stat("encode")
    assert (stat.count, stat.total, stat.worst) == (1, 0.25, 0.25)
    assert stat.mean == 0.25


def test_recordings_accumulate_and_keep_the_worst():
    m = M.Metrics()
    for seconds in (0.1, 0.5, 0.2):
        m.record("emulate", seconds)
    stat = m.stat("emulate")
    assert stat.count == 3
    assert stat.mean == pytest.approx(0.2667, abs=0.001)
    assert stat.worst == 0.5


def test_a_name_never_recorded_is_zero_rather_than_a_crash():
    stat = M.Metrics().stat("nothing")
    assert (stat.count, stat.total, stat.worst, stat.mean) == (0, 0.0, 0.0, 0.0)


def test_the_timer_records_a_block_that_raised():
    """The slow press that then failed is the one worth knowing about."""
    m = M.Metrics()
    with pytest.raises(ValueError):
        with m.timing("emulate"):
            raise ValueError("the core fell over")
    assert m.stat("emulate").count == 1


def test_timings_are_kept_apart_by_name():
    m = M.Metrics()
    m.record("core wait", 1.0)
    m.record("encode", 2.0)
    assert m.names == ["core wait", "encode"]
    assert m.stat("core wait").worst == 1.0
    assert m.stat("encode").worst == 2.0


# -- The lag sampler ----------------------------------------------------------


def test_a_small_lag_is_counted_and_says_nothing(caplog):
    m = M.Metrics()
    with caplog.at_level(logging.WARNING):
        m.note_lag(0.05)
    assert (m.lag_samples, m.bad_lag_samples) == (1, 0)
    assert m.worst_lag == 0.05
    assert not caplog.text


def test_a_lag_past_discords_window_is_counted_and_warned(caplog):
    """Three seconds is the whole budget for acknowledging a click."""
    m = M.Metrics()
    with caplog.at_level(logging.WARNING):
        m.note_lag(M.LAG_WARN_SECONDS + 0.5)
    assert (m.lag_samples, m.bad_lag_samples) == (1, 1)
    assert "late waking up" in caplog.text
    assert "three seconds" in caplog.text


def test_the_worst_lag_is_kept_not_the_latest():
    m = M.Metrics()
    for seconds in (0.2, 1.5, 0.1):
        m.note_lag(seconds)
    assert m.worst_lag == 1.5
    assert m.lag_samples == 3


async def test_the_sampler_records_and_stops_when_cancelled(monkeypatch):
    """It runs for ever, so the only way out is the cog cancelling it."""
    monkeypatch.setattr(M, "LAG_INTERVAL_SECONDS", 0.01)
    m = M.Metrics()
    task = asyncio.create_task(m.sample_lag())
    for _ in range(200):
        await asyncio.sleep(0.005)
        if m.lag_samples:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert m.lag_samples >= 1, "the sampler never recorded anything"


async def test_a_sampler_whose_recording_breaks_keeps_running(monkeypatch):
    """Sampling must not die of its own bookkeeping."""
    monkeypatch.setattr(M, "LAG_INTERVAL_SECONDS", 0.01)
    m = M.Metrics()
    calls = []

    def angry(seconds):
        calls.append(seconds)
        raise RuntimeError("bookkeeping fell over")

    m.note_lag = angry
    task = asyncio.create_task(m.sample_lag())
    for _ in range(200):
        await asyncio.sleep(0.005)
        if len(calls) >= 2:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 2, "it stopped at the first failure"


# -- What `[p]retrodiagnose` prints -------------------------------------------


def test_an_idle_bot_says_so_rather_than_printing_zeroes():
    assert M.describe_timings(M.Metrics()) == [
        "- Nothing has been pressed since the cog loaded."
    ]


def test_each_timing_reports_its_count_mean_and_worst():
    m = M.Metrics()
    m.record("encode", 0.100)
    m.record("encode", 0.300)
    line = M.describe_timings(m)[0]
    assert "encode" in line
    assert "2x" in line
    assert "mean 200ms" in line
    assert "worst 300ms" in line


def test_the_lag_line_only_appears_once_something_was_sampled():
    m = M.Metrics()
    m.record("encode", 0.1)
    assert not any("Event loop lag" in line for line in M.describe_timings(m))
    m.note_lag(0.4)
    line = next(line for line in M.describe_timings(m) if "Event loop lag" in line)
    assert "worst 400ms" in line
    assert "1 sample(s)" in line


def test_a_bad_lag_is_called_out_in_the_line(caplog):
    m = M.Metrics()
    with caplog.at_level(logging.WARNING):
        m.note_lag(M.LAG_WARN_SECONDS + 1)
    line = next(line for line in M.describe_timings(m) if "Event loop lag" in line)
    assert f"1 past {M.LAG_WARN_SECONDS:.0f}s" in line
