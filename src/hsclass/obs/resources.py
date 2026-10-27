"""The costs that are not tokens: wall time, and the memory floor.

``obs/cost.py`` prices model calls, which is the cost a classifier is usually
judged on. It is not the cost this pipeline actually incurs. Measured on the
test split, adding the cross-encoder reranker moved the numbers that matter
like this:

| | without | with |
| --- | ---: | ---: |
| tokens spent | 0 | 0 |
| dollars spent | $0.00 | $0.00 |
| per classification | 16ms | ~1,900ms |
| process working set | 472MB | **2.7GB** |

A ceiling denominated in dollars sees nothing at all there, and the failure
it would have caught was real: two evaluation runs at once exhausted a 16GB
machine and the second died inside onnxruntime with ``bad allocation``, an
error naming neither memory nor the component responsible.

So this module adds the two measurements that would have caught it, and
keeps them honest about a difference between them:

* **Latency is attributable to a request.** It is checked at each stage
  boundary - which bounds the stages *after* a breach, not the stage that
  caused it. Interrupting a running stage would need a separate thread or
  process and is not done, so a hung ``onnxruntime`` call hangs forever.
  The dollar ceiling has the same shape: it records, then checks.
* **Memory is not.** The 2.7GB is an arena ONNX reserves within the first
  twenty classifications and then holds flat for the life of the process.
  Charging it to whichever request happened to trigger the reservation would
  be a fiction, and charging each request a share of it would be another.
  It is recorded as a process-level gauge, labelled as one, and never
  enforced per request.

That asymmetry is the point of the module rather than an infelicity in it.
A budget that claims to attribute an unattributable cost invites exactly the
kind of confident wrong number this project keeps finding.
"""

from __future__ import annotations

import ctypes
import sys
import time
from dataclasses import dataclass


class LatencyBudgetExceeded(RuntimeError):
    """One request took longer than its budget allows."""

    def __init__(self, spent_ms: float, budget_ms: float, stage: str) -> None:
        super().__init__(
            f"request took {spent_ms:.0f}ms, over its {budget_ms:.0f}ms budget, "
            f"at stage {stage!r}"
        )
        self.spent_ms = spent_ms
        self.budget_ms = budget_ms
        self.stage = stage


#: ``PROCESS_MEMORY_COUNTERS``, written in plain ctypes types rather than
#: through ``ctypes.wintypes``.
#:
#: That is not a style preference. ``import ctypes.wintypes`` raises
#: ``ValueError: _type_ 'v' not supported`` on a non-Windows interpreter, at
#: *import* time - so a top-level import of it here took down this module,
#: ``obs/trace.py`` and ``pipeline/adjudicated.py`` with it, and with them
#: most of the test suite. The ``sys.platform`` guard below never got a
#: chance to run. The CI job is ``ubuntu-latest`` running ``pytest``, so this
#: broke a working workflow, which is the project's own rule about workflows
#: biting from the other end. The field widths are what wintypes would have
#: given: DWORD is a 32-bit unsigned long, SIZE_T is ``c_size_t``.
class _MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _windows_reader():
    """Bind ``GetProcessMemoryInfo``, or return None where it does not exist.

    ``argtypes`` is set deliberately rather than left to ctypes' defaults.
    Without it the process handle is truncated to 32 bits on a 64-bit
    build, the call fails, and the struct keeps its zeroed fields - so the
    reading comes back as 0MB and looks like a measurement rather than a
    failure. That cost an hour of believing memory was flat.
    """
    if not hasattr(ctypes, "WinDLL"):
        return None
    try:
        psapi = ctypes.WinDLL("psapi.dll")
        kernel32 = ctypes.WinDLL("kernel32.dll")
    except (OSError, AttributeError):
        return None
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_MemoryCounters), ctypes.c_ulong
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p

    def read() -> tuple[float, float] | None:
        counters = _MemoryCounters()
        counters.cb = ctypes.sizeof(_MemoryCounters)
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        if not ok:
            return None
        return (
            counters.WorkingSetSize / 1048576,
            counters.PeakWorkingSetSize / 1048576,
        )

    return read


_READ_MEMORY = _windows_reader() if sys.platform == "win32" else None


def process_memory_mb() -> tuple[float, float] | None:
    """Current and peak working set in MB, or None if it cannot be read.

    None rather than 0.0, because a zero here is indistinguishable from a
    small process and would be recorded as a fact.
    """
    return _READ_MEMORY() if _READ_MEMORY is not None else None


@dataclass
class Budget:
    """What one request is allowed to spend, in money and in time.

    Memory is absent by design - see the module docstring. A per-request
    memory ceiling over a process-wide arena would fail on whichever request
    arrived first and pass for every one after it.
    """

    usd: float | None = None
    latency_ms: float | None = None

    def check_latency(self, spent_ms: float, stage: str) -> None:
        """Raise if the request has already overrun, naming the stage.

        Post-hoc by construction: the caller invokes this between stages, so
        the breach is detected once its cost is paid. It stops the stages
        that would have followed, which with a reranker in propose is most
        of the remaining time, and it turns a slow tail into a typed error
        instead of a silent percentile.
        """
        if self.latency_ms is not None and spent_ms > self.latency_ms:
            raise LatencyBudgetExceeded(spent_ms, self.latency_ms, stage)


@dataclass
class ResourceGauge:
    """Process-level memory readings taken alongside a request.

    Memory only. An earlier version also carried a ``wall_ms``, which was
    two mistakes: it defaulted to ``0.0`` and so recorded a zero as a fact -
    the thing ``process_memory_mb`` returns None to avoid - and it put a
    second request duration in every trace beside ``Trace.latency_ms``,
    differing by about a millisecond with nothing to say which an auditor
    should believe. Timing belongs to the trace; this gauge is for the cost
    the trace could not otherwise see.
    """

    working_set_mb: float | None = None
    peak_working_set_mb: float | None = None

    @classmethod
    def sample(cls) -> ResourceGauge:
        reading = process_memory_mb()
        if reading is None:
            return cls()
        current, peak = reading
        return cls(working_set_mb=round(current, 1), peak_working_set_mb=round(peak, 1))

    def to_dict(self) -> dict:
        # Named to make the scope unmistakable in a trace: these belong to
        # the process, not to the request they were sampled beside.
        return {
            "process_working_set_mb": self.working_set_mb,
            "process_peak_working_set_mb": self.peak_working_set_mb,
        }


class Stopwatch:
    """Elapsed milliseconds since construction."""

    def __init__(self) -> None:
        self._t0 = time.perf_counter()

    @property
    def ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000
