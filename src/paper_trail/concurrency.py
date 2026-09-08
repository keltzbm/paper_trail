"""Generic concurrent batch-processing helper.

This module has no knowledge of PDFs, papers, or Springer — it simply
runs a worker function over a list of items on a thread pool and shows
a progress indicator. It exists so that ``organizer.py`` and ``utils.py``
don't each carry their own copy of the same ThreadPoolExecutor + tqdm
boilerplate.
"""

import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from typing import Callable, Iterable, TypeVar

try:
    from tqdm import tqdm

    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

T = TypeVar("T")
R = TypeVar("R")

# Hard ceiling on concurrency in adaptive mode, and the cap default_worker_count()
# itself never exceeds. Shared so the two stay consistent by construction.
MAX_ADAPTIVE_WORKERS = 32
MIN_ADAPTIVE_WORKERS = 2

# How often (seconds) the adaptive mode re-checks system load mid-run.
_DEFAULT_RECHECK_INTERVAL = 10.0

# Thresholds for the *ongoing* mid-run adjustment (see
# _AdaptiveLimiter._monitor_loop), expressed as load_1min / cpu_count.
# There's a deliberate wide "hold steady" band in between: a machine
# sitting at, say, 80-90% utilization is being used well, not struggling —
# reacting to that is exactly what causes oscillation (more workers -> load
# rises into the "high" zone -> fewer workers -> load falls back down ->
# more workers -> ...). Only genuine slack (comfortably under-utilized)
# earns more workers, and only genuine oversubscription (load meaningfully
# past 100% of core count, i.e. things actually queuing for CPU) triggers
# backing off. The two thresholds are intentionally asymmetric too: quick
# to back off when truly overloaded, slow/incremental to climb back up —
# the same shape as TCP's additive-increase/multiplicative-decrease, chosen
# for the same reason (fast reaction to real contention, no overshoot on
# the way back up).
_LOW_WATER_RATIO = 0.7
_HIGH_WATER_RATIO = 1.3
_STEP_UP = 2
_STEP_DOWN_FACTOR = 0.75


def default_worker_count() -> int:
    """A sensible thread-pool size based on CPU count and *current* load.

    Most per-item time in this codebase is spent waiting on network calls
    (Crossref/arXiv lookups), with some genuine CPU work mixed in (PDF
    rasterization, file hashing). Since I/O-bound threads spend most of
    their time waiting rather than competing for a CPU core, oversubscribing
    relative to *idle* core count is reasonable here — but not unboundedly.

    Factors in the current 1-minute load average (POSIX only — macOS/Linux,
    via ``os.getloadavg()``) to estimate how much CPU capacity is actually
    free right now, rather than assuming the whole machine is idle. Falls
    back to treating the whole machine as available on Windows (where
    ``getloadavg`` doesn't exist) or if it can't be read for any reason.

    This is a snapshot taken at the moment it's called. ``run_concurrent``
    calls this repeatedly over the course of a run (see ``_AdaptiveLimiter``)
    to actually track load as it changes, rather than freezing on one
    reading taken at the very start. Don't cache the result yourself, and
    never use it as a plain function-default expression (evaluated once at
    import time, not per call) — pass a ``None`` sentinel default instead
    and call this inside the function body.
    """
    cpu_count = os.cpu_count() or 4

    try:
        load_1min, _, _ = os.getloadavg()
        # Rough estimate of currently-idle capacity. Never let this
        # collapse below 1 even under heavy load, since this workload is
        # mostly I/O-bound and can still make some progress with a small
        # pool rather than none at all.
        headroom = max(1.0, cpu_count - load_1min)
    except (OSError, AttributeError):
        headroom = float(cpu_count)

    return max(MIN_ADAPTIVE_WORKERS, min(MAX_ADAPTIVE_WORKERS, int(headroom * 2)))


class _AdaptiveLimiter:
    """A concurrency gate whose permitted count changes at runtime.

    Python's ``ThreadPoolExecutor`` has no supported way to resize its
    worker count after creation. This works around that: a fixed-size pool
    of real OS threads is created (sized at the hard cap), but each worker
    must acquire a permit from this gate before doing its actual work and
    release it after. A background thread periodically re-checks system
    load and nudges the permitted count up or down — so *active*
    concurrency tracks load throughout a long run, without needing to
    touch the executor itself. Blocked threads cost nothing but a little
    memory; they aren't spinning.

    The adjustment is deliberately not "recompute a fresh target from
    scratch every check and jump straight to it" — that naively
    proportional approach oscillates (more workers raises load, which
    lowers workers, which drops load, which raises workers, ... forever).
    Instead it holds steady inside a wide, healthy-utilization deadband,
    and steps incrementally rather than jumping when it does react — see
    the threshold/step constants near the top of this module.

    Prints a line (via ``tqdm.write()`` if a progress bar is active, so it
    doesn't garble the bar) whenever the permitted count actually changes —
    silent otherwise, so a long run doesn't get spammed with a message on
    every recheck when nothing's different.
    """

    def __init__(self, initial_limit: int, recheck_interval: float = _DEFAULT_RECHECK_INTERVAL):
        self._limit = max(MIN_ADAPTIVE_WORKERS, min(MAX_ADAPTIVE_WORKERS, initial_limit))
        self._in_use = 0
        self._cond = threading.Condition()
        self._stop_event = threading.Event()
        self._monitor = threading.Thread(
            target=self._monitor_loop, args=(recheck_interval,), daemon=True
        )
        self._monitor.start()

    def _monitor_loop(self, recheck_interval: float) -> None:
        while not self._stop_event.wait(recheck_interval):
            cpu_count = os.cpu_count() or 4
            try:
                load_1min, _, _ = os.getloadavg()
            except (OSError, AttributeError):
                continue  # no load data on this platform (e.g. Windows) - nothing to react to

            load_ratio = load_1min / cpu_count

            with self._cond:
                current = self._limit

                if load_ratio < _LOW_WATER_RATIO:
                    new_limit = min(MAX_ADAPTIVE_WORKERS, current + _STEP_UP)
                    reason = "load has slack"
                elif load_ratio > _HIGH_WATER_RATIO:
                    new_limit = max(MIN_ADAPTIVE_WORKERS, min(current - 1, int(current * _STEP_DOWN_FACTOR)))
                    reason = "load too high"
                else:
                    #  healthy-utilization deadband - hold steady
                    new_limit = current
                    reason = None

                if new_limit != current:
                    self._limit = new_limit
                    self._cond.notify_all()
                    message = f"\tWorkers: {current} → {new_limit} ({reason})"
                    if HAS_TQDM:
                        tqdm.write(message)
                    else:
                        print(message)

    def acquire(self, timeout: float | None = None) -> None:
        """Blocks until a permit is free.

        Raises ``TimeoutError`` if ``timeout`` elapses first, or
        ``RuntimeError`` if the limiter is stopped while waiting — either
        way, the caller (a worker thread that hasn't started real work
        yet) can exit cleanly instead of blocking forever, which matters
        once ``stop()`` has been called at the end of a run.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._in_use >= self._limit:
                if self._stop_event.is_set():
                    raise RuntimeError("concurrency limiter stopped")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("timed out waiting for a concurrency slot")
                self._cond.wait(timeout=remaining)
            self._in_use += 1

    def release(self) -> None:
        with self._cond:
            self._in_use = max(0, self._in_use - 1)
            self._cond.notify_all()

    def stop(self) -> None:
        """Signals the monitor thread to stop and wakes any blocked acquire() calls."""
        self._stop_event.set()
        with self._cond:
            self._cond.notify_all()
        self._monitor.join(timeout=1.0)


def run_concurrent(
    items: Iterable[T],
    worker: Callable[[T], R],
    *,
    desc: str,
    unit: str,
    max_workers: int | None = None,
    on_error: Callable[[T, Exception], None] | None = None,
    timeout: float | None = None,
    poll_interval: float = 2.0,
) -> list[R | None]:
    """Runs ``worker`` over ``items`` concurrently with a progress indicator.

    Falls back to a plain carriage-return counter when tqdm isn't installed.

    Args:
        items: The items to process.
        worker: A function called once per item; may itself catch and
            report its own errors, or let them propagate to ``on_error``.
        desc: Short label shown on the progress bar / counter.
        unit: Unit label shown on the progress bar (e.g. "pdf", "folder").
        max_workers: If ``None`` (the default), concurrency *adapts* to
            system load over the course of the run — a background check
            every ~10s adjusts how many items may run at once (see
            ``default_worker_count()`` / ``_AdaptiveLimiter``), so a long
            batch responds to the machine getting busier or freeing up,
            rather than being locked to a snapshot taken once at the
            start. Pass an explicit integer to pin a fixed count instead
            (no adaptation — a plain fixed-size thread pool, matching the
            older behavior exactly).
        on_error: Optional callback invoked with ``(item, exception)`` if
            ``worker`` raises, or if it exceeds ``timeout``. If omitted,
            exceptions/timeouts are swallowed and the result is ``None``.
        timeout: If set, any single item still running (not queued — see
            below) after this many seconds is reported as failed and the
            progress indicator moves on, rather than waiting on it forever.
            The clock starts when a worker thread actually *begins*
            running the item — i.e. after it has both cleared the queue
            and (in adaptive mode) actually acquired a concurrency permit —
            not when it was merely submitted; time spent only queued or
            waiting for a permit must not count against it. Also bounds
            how long a worker will wait for a permit in the first place, so
            a persistently overloaded machine can't block a thread forever.
            Python threads can't be forcibly killed, so a thread already
            doing real work keeps running in the background past this
            point — this only stops *waiting* on it. In practice that
            means: the reported count for that item may be wrong if it
            later succeeds on its own, and the process can take a little
            longer to fully exit than the printed summary suggests. It's a
            deliberate tradeoff to keep one genuinely stuck item (e.g. a
            slow network read, or a scanned PDF that hangs in OCR) from
            freezing the entire run. ``None`` (the default) waits
            indefinitely, matching the old behavior.
        poll_interval: How often (seconds) to check elapsed time against
            ``timeout`` while waiting. Irrelevant if ``timeout`` is None.

    Returns:
        Results in completion order (not input order). Failed or timed-out
        items contribute ``None`` to the list.
    """
    items = list(items)
    results: list[R | None] = []

    limiter: _AdaptiveLimiter | None = None
    if max_workers is None:
        pool_size = min(MAX_ADAPTIVE_WORKERS, max(1, len(items)))
        limiter = _AdaptiveLimiter(initial_limit=default_worker_count())
    else:
        pool_size = max(1, max_workers)

    executor = ThreadPoolExecutor(max_workers=pool_size)
    try:
        # Populated by the worker itself, from inside the worker thread,
        # the moment it actually starts running — not by the submitting
        # loop below, which would instead record roughly "time zero" for
        # every item regardless of how long it then sits queued (or, in
        # adaptive mode, waiting for a concurrency permit) for a free slot.
        start_times: dict[int, float] = {}
        start_times_lock = threading.Lock()

        def make_timed_worker(index: int):
            def timed_worker(item: T) -> R:
                if limiter is not None:
                    limiter.acquire(timeout=timeout)
                try:
                    with start_times_lock:
                        start_times[index] = time.monotonic()
                    return worker(item)
                finally:
                    if limiter is not None:
                        limiter.release()
            return timed_worker

        futures = {}
        indices = {}
        for i, item in enumerate(items):
            future = executor.submit(make_timed_worker(i), item)
            futures[future] = item
            indices[future] = i

        pending = set(futures)
        gave_up_on = set()
        progress = (
            tqdm(total=len(items), desc=desc, unit=unit, leave=True, mininterval=0)
            if HAS_TQDM
            else None
        )
        completed = 0

        def _report(item: T, value=None, exc: Exception | None = None) -> None:
            nonlocal completed
            if exc is not None and on_error:
                on_error(item, exc)
            results.append(value)
            completed += 1
            if progress:
                progress.update(1)
            else:
                print(f"{desc} [{completed}/{len(items)}]...", end="\r", flush=True)

        while pending:
            done, pending = wait_futures(
                pending, timeout=poll_interval if timeout else None,
                return_when=FIRST_COMPLETED,
            )

            for future in done:
                item = futures[future]
                try:
                    _report(item, value=future.result())
                except Exception as exc:  # noqa: BLE001 - reported via on_error
                    _report(item, exc=exc)

            if timeout is not None:
                now = time.monotonic()
                with start_times_lock:
                    started = dict(start_times)
                overdue = {
                    f
                    for f in pending
                    if f not in gave_up_on
                    and indices[f] in started
                    and now - started[indices[f]] > timeout
                }
                for future in overdue:
                    gave_up_on.add(future)
                    item = futures[future]
                    _report(
                        item,
                        exc=TimeoutError(
                            f"still running after {timeout:.0f}s — no longer "
                            "waiting on it (it may finish in the background)"
                        ),
                    )
                pending -= overdue

        if progress:
            progress.close()
        elif items:
            print()
    finally:
        # Stop the limiter first (wakes any thread still blocked waiting
        # for a permit so it can exit cleanly instead of hanging forever
        # once nothing will ever release a slot for it again), *then*
        # shut down the executor without waiting (wait=False): every item
        # has already been reported by this point (completed, or given up
        # on after a timeout) — don't make the caller sit through however
        # long any still-running straggler threads take just to get
        # results/print a summary it's already earned. Python can't
        # forcibly kill those threads either way; this just stops *this
        # function* from blocking on them. (The Python process itself may
        # still pause briefly at final exit if any are still running —
        # that's a separate, unavoidable interpreter-level wait, not this
        # function hanging.)
        if limiter is not None:
            limiter.stop()
        executor.shutdown(wait=False)

    return results
