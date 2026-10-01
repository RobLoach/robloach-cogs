"""
Where a press spends its time, and whether the event loop is keeping up.

Three questions have needed this and been answered by hand each time: how long
a press really takes, which part of it is expensive, and whether the bot's
event loop is running on time. The third is the one that matters most and is
the hardest to guess at -- a click Discord sent more than three seconds ago
cannot be acknowledged at all (it answers ``10062 Unknown interaction``, and
every edit through that token then answers ``10015``), and the only way to tell
a slow bot from a slow network is to measure the bot.

Kept deliberately small:

* **no history.** Count, total and maximum per name, which is four floats
  whatever the bot does. A ring buffer of recent presses would answer nicer
  questions and would also grow with traffic, and nothing here is worth a
  megabyte of a bot's memory.
* **no dependency and no task of its own** except the lag sampler, which is
  one coroutine sleeping in a loop.
* **never raises.** Every call site is on the press path. A metric that can
  fail a press is worse than no metric.

Read by `[p]retrodiagnose`. Nothing else consumes it, and nothing is persisted:
a reload starts the numbers again, which is the right scope for "is this bot
healthy *now*".
"""

import asyncio
import contextlib
import logging
import time
import typing

log = logging.getLogger("red.robloach.retro")

#: How often the lag sampler wakes. Long enough to cost nothing, short enough
#: that a few minutes of running is a useful sample.
LAG_INTERVAL_SECONDS = 5.0

#: A sample this far past its due time is worth saying out loud, because it is
#: past the three seconds Discord allows for acknowledging a click: a press
#: landing in a stall this long is one the player sees fail. Logged once per
#: occurrence at warning, which is rare by construction -- a bot doing this
#: regularly has a problem worth the log lines.
LAG_WARN_SECONDS = 3.0


class Stat(typing.NamedTuple):
    """One timed thing: how often, how long in total, and the worst one."""

    count: int
    total: float
    worst: float

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0


class Metrics:
    """Running totals for the things a press spends time on.

    One instance per cog, on ``Retro.metrics``. Not thread-safe and does not
    need to be: every ``record`` call happens on the event loop, including the
    ones that time work done in a worker thread -- the timing is taken around
    the ``await``, not inside the thread.
    """

    def __init__(self) -> None:
        self._stats: typing.Dict[str, typing.List[float]] = {}
        #: Worst scheduling delay the sampler has seen, in seconds.
        self.worst_lag: float = 0.0
        #: How many samples were past LAG_WARN_SECONDS.
        self.bad_lag_samples: int = 0
        self.lag_samples: int = 0

    def record(self, name: str, seconds: float) -> None:
        """Note one occurrence. Never raises."""
        try:
            entry = self._stats.get(name)
            if entry is None:
                self._stats[name] = [1, seconds, seconds]
            else:
                entry[0] += 1
                entry[1] += seconds
                if seconds > entry[2]:
                    entry[2] = seconds
        except Exception:  # pragma: no cover - a metric must never cost a press
            log.debug("Could not record the %s timing.", name, exc_info=True)

    @contextlib.contextmanager
    def timing(self, name: str) -> typing.Iterator[None]:
        """Time a block, whatever it does or raises.

        The failure is timed too, on purpose: a press that took four seconds
        and then failed is exactly the press worth knowing about, and leaving
        it out would make the numbers describe only the happy path.
        """
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, time.perf_counter() - start)

    def stat(self, name: str) -> Stat:
        count, total, worst = self._stats.get(name, (0, 0.0, 0.0))
        return Stat(count, total, worst)

    @property
    def names(self) -> typing.List[str]:
        return sorted(self._stats)

    def note_lag(self, seconds: float) -> None:
        """Record one scheduling delay from the sampler."""
        self.lag_samples += 1
        if seconds > self.worst_lag:
            self.worst_lag = seconds
        if seconds >= LAG_WARN_SECONDS:
            self.bad_lag_samples += 1
            log.warning(
                "The Retro event loop was %.1fs late waking up. A click that "
                "arrives during a stall this long cannot be acknowledged in "
                "the three seconds Discord allows.",
                seconds,
            )

    async def sample_lag(self) -> None:
        """Measure how late the event loop is, for ever. Never raises.

        The whole of it: sleep a known time, see how long that really took.
        Anything above zero is the loop being busy elsewhere -- an emulated
        press holding the GIL, a slow Discord request, another cog. This is
        the cheapest possible answer to "is it the bot or the network?", and
        that question has come up once already in production without one.

        Cancelled with the cog; see ``Retro.cog_unload``.
        """
        while True:
            before = time.monotonic()
            try:
                await asyncio.sleep(LAG_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise
            late = (time.monotonic() - before) - LAG_INTERVAL_SECONDS
            try:
                self.note_lag(max(0.0, late))
            except Exception:  # pragma: no cover - sampling must not die
                log.debug("Could not record the event loop lag.", exc_info=True)


def describe_timings(metrics: Metrics) -> typing.List[str]:
    """`[p]retrodiagnose`'s Timing section, as lines.

    Says "nothing recorded yet" rather than a table of zeroes, because a bot
    that has not been played since it loaded is the ordinary case and a row of
    0.0ms reads as a broken measurement rather than an idle bot.
    """
    lines = []
    if not metrics.names:
        lines.append("- Nothing has been pressed since the cog loaded.")
    for name in metrics.names:
        stat = metrics.stat(name)
        lines.append(
            f"- {name}: {stat.count}x, mean {stat.mean * 1000:.0f}ms, "
            f"worst {stat.worst * 1000:.0f}ms"
        )
    if metrics.lag_samples:
        lines.append(
            f"- Event loop lag: worst {metrics.worst_lag * 1000:.0f}ms over "
            f"{metrics.lag_samples} sample(s)"
            + (
                f", **{metrics.bad_lag_samples} past {LAG_WARN_SECONDS:.0f}s**"
                if metrics.bad_lag_samples
                else ""
            )
        )
    return lines
