"""
Panic-mode renderer.
Press Escape to toggle between the reader and a scrollable fake Python module
that looks like ordinary backend work.
"""

import shutil

from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text

from renderer import _home, _clear_below

console = Console()

FAKE_FILE = "src/core/pipeline/dispatcher.py"

# ---------------------------------------------------------------------------
# ~220 lines of convincing, deliberately ambiguous Python.
# Looks like internal infra / data-pipeline code — impossible to summarise
# at a glance.
# ---------------------------------------------------------------------------
FAKE_CODE = '''\
from __future__ import annotations

import asyncio
import hashlib
import logging
import struct
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterator,
    List,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)

_MAGIC   = b"\\xde\\xad\\xbe\\xef"
_VERSION = (2, 4, 1)
_MAX_RETRY = 5
_DEFAULT_TTL = 86_400          # 24 h


class State(Enum):
    IDLE     = auto()
    RUNNING  = auto()
    DRAINING = auto()
    STOPPED  = auto()
    FAULTED  = auto()


@dataclass
class Config:
    workers:       int   = 4
    batch_size:    int   = 256
    retry_limit:   int   = 3
    timeout_ms:    int   = 5_000
    backoff:       float = 1.5
    enable_ckpt:   bool  = True
    ckpt_interval: int   = 100
    codec:         str   = "lz4"
    sink:          Path  = Path("./sink")
    _tag: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        blob = f"{self.workers}:{self.batch_size}:{self.timeout_ms}"
        self._tag = hashlib.sha1(blob.encode()).hexdigest()[:8]

    @property
    def tag(self) -> str:
        return self._tag

    def validate(self) -> None:
        if self.workers < 1:
            raise ValueError("workers must be >= 1")
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if self.backoff < 1.0:
            raise ValueError("backoff factor must be >= 1.0")


class RingBuffer:
    """Lock-free circular buffer; evicts oldest entry on overflow."""

    __slots__ = ("_buf", "_cap", "_head", "_tail", "_size")

    def __init__(self, capacity: int) -> None:
        self._buf: list = [None] * capacity
        self._cap  = capacity
        self._head = self._tail = self._size = 0

    def push(self, item: Any) -> Optional[Any]:
        evicted: Optional[Any] = None
        if self._size == self._cap:
            evicted    = self._buf[self._tail]
            self._tail = (self._tail + 1) % self._cap
        else:
            self._size += 1
        self._buf[self._head] = item
        self._head = (self._head + 1) % self._cap
        return evicted

    def drain(self) -> List[Any]:
        out = list(self)
        self._head = self._tail = self._size = 0
        return out

    def __iter__(self) -> Iterator[Any]:
        idx = self._tail
        for _ in range(self._size):
            yield self._buf[idx]
            idx = (idx + 1) % self._cap

    def __len__(self) -> int:
        return self._size

    def __repr__(self) -> str:
        return f"RingBuffer(cap={self._cap}, size={self._size})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _backoff(attempt: int, base: float = 0.05, factor: float = 1.5, cap: float = 30.0) -> float:
    return min(base * (factor ** attempt), cap)


def _pack_header(seq: int, length: int, flags: int = 0) -> bytes:
    return _MAGIC + struct.pack(">HHB", seq & 0xFFFF, length & 0xFFFF, flags & 0xFF)


def sliding_window(
    seq: list,
    size: int,
    step: int = 1,
) -> Generator[list, None, None]:
    if size > len(seq):
        return
    for i in range(0, len(seq) - size + 1, step):
        yield seq[i : i + size]


def normalise(
    values: List[float],
    *,
    lo: float = 0.0,
    hi: float = 1.0,
) -> List[float]:
    mn, mx = min(values), max(values)
    span = mx - mn or 1.0
    return [lo + (v - mn) / span * (hi - lo) for v in values]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class Stage:
    """Single processing unit in a Pipeline."""

    def __init__(self, name: str, fn: Callable, weight: float = 1.0) -> None:
        self.name   = name
        self._fn    = fn
        self.weight = weight
        self._calls   = 0
        self._elapsed = 0.0

    def __call__(self, data: Any) -> Any:
        t0 = time.perf_counter()
        result = self._fn(data)
        self._elapsed += time.perf_counter() - t0
        self._calls   += 1
        return result

    @property
    def avg_ms(self) -> float:
        return (self._elapsed / self._calls * 1_000) if self._calls else 0.0

    def reset(self) -> None:
        self._calls = self._elapsed = 0


class Pipeline:
    """Ordered chain of Stages applied to a single data object."""

    def __init__(self, name: str = "default") -> None:
        self.name = name
        self._stages: List[Stage] = []

    def add(self, name: str, fn: Callable, weight: float = 1.0) -> "Pipeline":
        self._stages.append(Stage(name, fn, weight))
        return self

    def run(self, data: Any) -> Any:
        for stage in self._stages:
            data = stage(data)
        return data

    def profile(self) -> Dict[str, float]:
        return {s.name: round(s.avg_ms, 3) for s in self._stages}

    def reset(self) -> None:
        for s in self._stages:
            s.reset()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class Dispatcher:
    """Async dispatcher with retry, back-pressure, and checkpointing."""

    def __init__(self, cfg: Config) -> None:
        cfg.validate()
        self._cfg   = cfg
        self._q: asyncio.Queue = asyncio.Queue(maxsize=cfg.batch_size * 2)
        self._state = State.IDLE
        self._stats: Dict[str, int] = defaultdict(int)
        self._hooks: List[Callable] = []
        self._buf   = RingBuffer(capacity=512)

    # -- public ----------------------------------------------------------------

    def hook(self, fn: Callable) -> Callable:
        """Register a post-dispatch callback (decorator-friendly)."""
        self._hooks.append(fn)
        return fn

    async def submit(self, item: Dict[str, Any]) -> None:
        await self._q.put(item)

    async def run(self) -> None:
        self._state = State.RUNNING
        try:
            while self._state is State.RUNNING:
                item = await asyncio.wait_for(self._q.get(), timeout=1.0)
                await self._dispatch(item)
                self._q.task_done()
        except asyncio.TimeoutError:
            pass
        finally:
            self._state = State.STOPPED

    async def run_batch(self, items: List[Dict]) -> Tuple[int, int]:
        ok = fail = 0
        for i, item in enumerate(items):
            if await self._dispatch(item):
                ok += 1
            else:
                fail += 1
            if self._cfg.enable_ckpt and i % self._cfg.ckpt_interval == 0:
                await self._checkpoint(i)
        return ok, fail

    @property
    def stats(self) -> Dict[str, int]:
        return dict(self._stats)

    @property
    def state(self) -> State:
        return self._state

    # -- internal --------------------------------------------------------------

    async def _dispatch(self, item: Dict[str, Any]) -> bool:
        for attempt in range(self._cfg.retry_limit):
            try:
                await asyncio.wait_for(
                    self._invoke(item),
                    timeout=self._cfg.timeout_ms / 1_000,
                )
                self._stats["ok"] += 1
                return True
            except asyncio.TimeoutError:
                self._stats["timeout"] += 1
                logger.warning("timeout attempt=%d tag=%s", attempt, self._cfg.tag)
            except Exception as exc:  # noqa: BLE001
                self._stats["err"] += 1
                logger.error("dispatch error attempt=%d: %s", attempt, exc)
            await asyncio.sleep(_backoff(attempt, factor=self._cfg.backoff))
        self._state = State.FAULTED
        return False

    async def _invoke(self, item: Dict[str, Any]) -> None:
        for hook in self._hooks:
            res = hook(item)
            if asyncio.iscoroutine(res):
                await res
        self._buf.push(item)

    async def _checkpoint(self, idx: int) -> None:
        path = self._cfg.sink / f"{self._cfg.tag}_{idx:06d}.bin"
        logger.debug("ckpt → %s", path)
        # TODO: serialise buffer snapshot


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

def build_pipeline(hooks: Optional[List[Callable]] = None) -> Pipeline:
    p = Pipeline("main")
    p.add("validate",  _validate)
    p.add("transform", _transform)
    p.add("enrich",    _enrich)
    if hooks:
        for h in hooks:
            p.add(h.__name__, h)
    return p


def _validate(data: Any) -> Any:
    if not isinstance(data, dict):
        raise TypeError(f"expected dict, got {type(data).__name__}")
    return data


def _transform(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


def _enrich(data: Dict[str, Any]) -> Dict[str, Any]:
    data.setdefault("_ts", time.time())
    data.setdefault("_v",  "{}.{}.{}".format(*_VERSION))
    return data
'''


def render_panic(scroll_offset: int) -> int:
    """
    Render the panic-mode fake code view.
    Returns the number of visible code lines (for scroll clamping).
    """
    term_w, term_h = shutil.get_terminal_size((120, 40))

    # 3 UI rows: tab bar + status bar + hint
    visible = max(5, term_h - 3)

    all_lines = FAKE_CODE.splitlines()
    total = len(all_lines)

    page_lines = all_lines[scroll_offset: scroll_offset + visible]
    while len(page_lines) < visible:
        page_lines.append('')

    syntax = Syntax(
        '\n'.join(page_lines),
        'python',
        theme='monokai',
        line_numbers=True,
        start_line=scroll_offset + 1,
    )

    fname = FAKE_FILE.split('/')[-1]
    at_end = scroll_offset + visible >= total

    # Tab bar — slightly different set from the reader tabs
    tabs = Text()
    tabs.append('  config.py  ',      style='dim on #2d2d2d')
    tabs.append(f'  {fname}  ',       style='bold white on #1e1e1e')
    tabs.append('  utils.py  ',       style='dim on #2d2d2d')
    tabs.append('  test_core.py  ',   style='dim on #2d2d2d')
    tabs.append('  __init__.py  ',    style='dim on #2d2d2d')

    status = Text()
    status.append('  ⎇ main  ',              style='bold on #0e639c')
    status.append(f'  {fname}  ',            style='on #37373d')
    status.append('  Python 3.11  ',         style='on #252526')
    status.append('  UTF-8  ',               style='on #252526')
    status.append(f'  Ln {scroll_offset + 1}/{total}, Col 1  ', style='on #252526')
    if at_end:
        status.append('  END  ', style='bold on #4ec9b0')

    hint = Text()
    hint.append(
        '  j/↓ k/↑ scroll   d/u half-page   Space/b page   Esc return   q quit',
        style='dim',
    )

    _home()
    console.print(tabs)
    console.print(syntax)
    console.print(status)
    console.print(hint)
    _clear_below()

    return visible
