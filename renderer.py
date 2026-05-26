import hashlib
import os
import re
import shutil
import textwrap
import time as _time
from typing import List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich import box

console = Console()

_HEADER_LINES    = 5
_FOOTER_LINES    = 4
_UI_LINES        = 3   # tab bar + status bar + hint line
_OVERHEAD        = _HEADER_LINES + _FOOTER_LINES + _UI_LINES  # 12
_LOG_OVERHEAD    = 6   # 1 header line + 5 log entries
_ACTIVITY_OVERHEAD = 1

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_LOG_POOL = [
    "[INFO]  server started on :8080",
    "[INFO]  connected to database (pool=10)",
    "[DEBUG] initializing cache layer",
    "[INFO]  GET /api/v2/entries — 200 OK (12ms)",
    "[INFO]  POST /api/v2/batch — 200 OK (34ms)",
    "[DEBUG] cache hit ratio: 0.87",
    "[INFO]  background worker started (pid=4821)",
    "[DEBUG] flushing write buffer (47 items)",
    "[INFO]  GET /api/v2/status — 200 OK (3ms)",
    "[DEBUG] reindex triggered: 1,204 documents",
    "[INFO]  PUT /api/v2/config — 204 No Content (8ms)",
    "[DEBUG] memory usage: 142 MB / 512 MB",
    "[INFO]  GET /api/v2/search?q=process — 200 OK (29ms)",
    "[DEBUG] query plan: index scan (cost=0.43..8.21)",
    "[INFO]  DELETE /api/v2/cache/stale — 200 OK (51ms)",
    "[DEBUG] connection pool: 7/10 active",
    "[WARN]  slow query detected (>100ms): batch_insert",
    "[INFO]  GET /api/v2/entries/142 — 200 OK (7ms)",
    "[DEBUG] checkpoint written to disk",
    "[INFO]  worker heartbeat OK (uptime: 3h 14m)",
    "[DEBUG] evicting 12 stale cache entries",
    "[INFO]  GET /api/v2/metrics — 200 OK (2ms)",
    "[DEBUG] pipeline flushed: batch_id=88",
    "[INFO]  PATCH /api/v2/entries/142 — 200 OK (19ms)",
    "[DEBUG] state machine: ACTIVE → PROCESSING",
    "[INFO]  GET /api/v2/entries — 200 OK (11ms)",
    "[DEBUG] buffer snapshot created (147 bytes)",
    "[INFO]  background sync complete (3 items)",
    "[DEBUG] gc: collected 0 objects",
    "[INFO]  GET /api/v2/status — 200 OK (2ms)",
]

_DEBUG_VARS = [
    ("_buffer",    lambda idx, off: f"Buffer[{idx * 7 + max(0, off) // 3} items]"),
    ("_state",     lambda idx, off: "ProcessingState.ACTIVE"),
    ("_ctx",       lambda idx, off: "Context(valid=True, depth=3)"),
    ("_processed", lambda idx, off: str(idx * 1000 + off * 7)),
    ("_errors",    lambda idx, off: "0"),
    ("_cache",     lambda idx, off: f"{{size: {off * 3 % 128}, ttl: 300}}"),
    ("_queue",     lambda idx, off: f"deque([...], maxlen={idx * 4 + 32})"),
    ("_results",   lambda idx, off: f"[{idx * 3 + 1} items]"),
]


def content_line_count(term_h: int, extra_overhead: int = 0) -> int:
    return max(5, term_h - _OVERHEAD - 2 - extra_overhead)


def mode_overhead(log: bool = False, activity: bool = False) -> int:
    return (_LOG_OVERHEAD if log else 0) + (_ACTIVITY_OVERHEAD if activity else 0)


def wrap_width(term_w: int, mode: str = 'normal') -> int:
    gutter = 8   # Rich line-number column
    indent = 4
    if mode == 'debug':
        term_w = int(term_w * 0.62)
    elif mode == 'diff':
        indent += 5  # '+    ' prefix
    return max(30, term_w - gutter - indent - 2)


# ── Fake path helpers ────────────────────────────────────────────────────────

def _slug(s: str) -> str:
    s = re.sub(r'[^\w\s]', '', s.lower())
    return re.sub(r'\s+', '_', s.strip())[:25] or 'module'


def fake_path(book_title: str, chapter_title: str) -> Tuple[str, str]:
    book_slug = _slug(book_title)
    ch_slug   = _slug(chapter_title)
    dirs = ['src/core', 'src/utils', 'lib/analysis', 'src/modules', 'core/processing']
    idx  = int(hashlib.md5(book_slug.encode()).hexdigest(), 16) % len(dirs)
    fp   = f"{dirs[idx]}/{book_slug}/{ch_slug}.py"
    return fp, f"{ch_slug}.py"


def classname(chapter_title: str) -> str:
    words = re.sub(r'[^\w\s]', '', chapter_title).split()
    meaningful = [w for w in words if not w.isdigit() and len(w) > 2]
    name = ''.join(w.capitalize() for w in meaningful[:3])
    return name or 'DocumentProcessor'


# ── Text wrapping ────────────────────────────────────────────────────────────

def wrap_chapter(text: str, width: int) -> List[str]:
    result: List[str] = []
    for para in re.split(r'\n\n+', text):
        para = para.strip()
        if not para:
            result.append('')
            continue
        if '\n' in para:
            for line in para.splitlines():
                line = line.strip()
                if line:
                    result.append(line)
        else:
            wrapped = textwrap.wrap(para, width)
            result.extend(wrapped or [''])
        result.append('')
    return result


# ── Per-line code transforms ─────────────────────────────────────────────────

_BETWEEN_FORMS = [
    lambda i: f'    result = _pipeline.flush(batch_id={i})',
    lambda i:  '    _counter += 1',
    lambda i:  '    log.debug("checkpoint: %d", _processed)',
    lambda i:  '    _state.advance(force=False)',
    lambda i:  '    assert _ctx.is_valid(), "state error"',
    lambda i:  '    yield _buffer.snapshot()',
    lambda i:  '    _result = dict(zip(_keys, _values))',
    lambda i:  '    _cache.update({"ts": time.time()})',
]


def _code_line(text: str, idx: int) -> str:
    if not text.strip():
        return _BETWEEN_FORMS[idx % len(_BETWEEN_FORMS)](idx)
    return f'    # {text}'


# ── Diff & debug helpers ─────────────────────────────────────────────────────

def _short_hash(seed: int, variant: int) -> str:
    return hashlib.md5(f'{seed}-{variant}'.encode()).hexdigest()[:7]


def _fake_func(chapter_idx: int) -> str:
    funcs = [
        'process_batch', 'flush_buffer', 'validate_state', 'advance_pipeline',
        'sync_context',  'rebuild_index', 'emit_event',    'checkpoint_write',
    ]
    return funcs[chapter_idx % len(funcs)]


def _diff_code(fp: str, page: List[str], base_ln: int, chapter_idx: int) -> str:
    h1 = _short_hash(chapter_idx, 0)
    h2 = _short_hash(chapter_idx, 1)
    fn = _fake_func(chapter_idx)
    n  = len(page)
    lines = [
        f'diff --git a/{fp} b/{fp}',
        f'index {h1}..{h2} 100644',
        f'--- a/{fp}',
        f'+++ b/{fp}',
        f'@@ -{base_ln},{n} +{base_ln},{n + 2} @@ def {fn}(self):',
        f'-    pass',
        f'-    # TODO: implement',
    ]
    for ln in page:
        lines.append(f'+    {ln}' if ln.strip() else '+')
    return '\n'.join(lines)


def _debug_panel(chapter_idx: int, line_offset: int) -> Panel:
    t = Text()
    t.append('  VARIABLES\n', style='bold #569cd6')
    t.append('  ' + '─' * 22 + '\n', style='dim')
    for name, val_fn in _DEBUG_VARS:
        val = val_fn(chapter_idx, line_offset)
        t.append(f'  {name}\n', style='#9cdcfe')
        t.append(f'    {val}\n', style='#ce9178')
    t.append('\n  CALL STACK\n', style='bold #569cd6')
    t.append('  ' + '─' * 22 + '\n', style='dim')
    for frame in [
        f'process_batch (l.{282 + chapter_idx * 3})',
        f'flush_buffer  (l.{195 + chapter_idx})',
        'run_pipeline  (l.87)',
        '__main__      (l.12)',
    ]:
        t.append(f'  {frame}\n', style='dim #aaaaaa')
    return Panel(t, title='[dim]Debug[/dim]', border_style='dim #569cd6', box=box.SIMPLE)


def _log_panel(tick: int, term_w: int = 100) -> Text:
    t = Text()
    sep = '─' * max(10, term_w - 22)
    t.append('  # server output  ', style='dim #6a9955')
    t.append(sep + '\n', style='dim #444444')
    for i in range(5):
        entry  = _LOG_POOL[(tick + i) % len(_LOG_POOL)]
        fake_h = 14 + (tick + i) // 60 % 6
        fake_m = (tick + i) % 60
        fake_s = (tick + i) * 7 % 60
        ts     = f'{fake_h:02d}:{fake_m:02d}:{fake_s:02d}'
        if '[WARN]' in entry:
            style, level = '#ce9178',    'WARN '
        elif '[DEBUG]' in entry:
            style, level = 'dim #888888', 'DEBUG'
        else:
            style, level = '#888888',    'INFO '
        msg = re.sub(r'\[(?:INFO|DEBUG|WARN)\]\s+', '', entry)
        t.append(f'  {ts}  {level}  {msg}\n', style=style)
    return t


def _activity_text(mono_now: float) -> Text:
    t      = Text()
    period = int(mono_now) % 70
    frame  = int(mono_now * 8) % len(_SPINNER)
    sp     = _SPINNER[frame]
    if period < 15:
        t.append(f'  {sp} Indexing workspace...', style='dim #4ec9b0')
    elif period < 25:
        t.append(f'  {sp} Running tests (47)...', style='dim #dcdcaa')
    elif period < 35:
        t.append('  ✓ Tests: 47 passed, 0 failed', style='dim #6a9955')
        t.append(f'   (last run: {int(mono_now) % 7 + 1}m ago)', style='dim')
    elif period < 50:
        pct  = min(99, int((period - 35) * 6.5))
        done = pct // 5
        bar  = '=' * done + ('>' if pct < 100 else '') + ' ' * (19 - done)
        t.append(f'  {sp} Building... [{bar}] {pct}%', style='dim #dcdcaa')
    elif period < 60:
        t.append('  ✓ Build succeeded   0 errors, 0 warnings', style='dim #6a9955')
    elif period < 67:
        t.append('  ⚠  Pylance: 2 warnings, 0 errors', style='dim #ce9178')
    else:
        t.append('  ◆ Copilot: 1 suggestion available', style='dim #569cd6')
    return t


# ── Renderers ────────────────────────────────────────────────────────────────

def render_page(
    book_title: str,
    chapter_title: str,
    wrapped: List[str],
    line_offset: int,
    chapter_idx: int,
    total_chapters: int,
    resumed: bool = False,
    high_mode: bool = False,
    diff_mode: bool = False,
    debug_mode: bool = False,
    log_tick: Optional[int] = None,
    show_activity: bool = False,
) -> int:
    """Draw one page. Returns the number of content lines shown."""
    term_w, term_h = shutil.get_terminal_size((120, 40))
    extra   = ((_LOG_OVERHEAD if log_tick is not None else 0)
               + (_ACTIVITY_OVERHEAD if show_activity else 0))
    n_lines = content_line_count(term_h, extra)

    fp, fname = fake_path(book_title, chapter_title)
    cls       = classname(chapter_title)
    base_ln   = 280 + chapter_idx * 170

    page = wrapped[line_offset: line_offset + n_lines]
    while len(page) < n_lines:
        page.append('')

    if diff_mode:
        code = _diff_code(fp, page, base_ln, chapter_idx)
        lang = 'diff'
    elif high_mode:
        body = '\n'.join(_code_line(ln, i) for i, ln in enumerate(page))
        code = (
            f'# {fp}\n'
            f'# Analysis pipeline — {chapter_title[:45]}\n'
            f'\n'
            f'class {cls}:\n'
            f'\n'
            f'{body}\n'
            f'\n'
            f'    def __init__(self) -> None:\n'
            f'        self._cache: dict = {{}}\n'
            f'        self._initialized: bool = True\n'
        )
        lang = 'python'
    else:
        safe     = [ln.replace('"""', '"') for ln in page]
        indented = '\n'.join(f'    {ln}' for ln in safe)
        code = (
            f'# {fp}\n'
            f'# Documentation module — auto-generated\n'
            f'\n'
            f'class {cls}:\n'
            f'    """\n'
            f'{indented}\n'
            f'    """\n'
            f'\n'
            f'    def __init__(self):\n'
            f'        self._initialized = True\n'
        )
        lang = 'python'

    syntax = Syntax(code, lang, theme='monokai', line_numbers=True, start_line=base_ln)

    total    = len(wrapped)
    progress = min(100, int(((line_offset + n_lines) / max(total, 1)) * 100))
    cur_ln   = base_ln + _HEADER_LINES + line_offset
    at_end   = line_offset + n_lines >= total

    # Tab bar — bullet (•) signals unsaved changes
    tabs = Text()
    tabs.append('  main.py  ',   style='dim on #2d2d2d')
    tabs.append('  config.py  ', style='dim on #2d2d2d')
    tabs.append(f'  {fname} •  ', style='bold white on #1e1e1e')
    tabs.append('  utils.py  ',  style='dim on #2d2d2d')
    tabs.append('  tests.py  ',  style='dim on #2d2d2d')

    # Status bar
    status = Text()
    status.append('  ⎇ main  ',                                style='bold on #0e639c')
    status.append(f'  {fname}  ',                              style='on #37373d')
    status.append('  Python 3.11  ',                           style='on #252526')
    status.append('  UTF-8  ',                                 style='on #252526')
    status.append(f'  Ch {chapter_idx + 1}/{total_chapters}  ', style='on #252526')
    status.append(f'  Ln {cur_ln}, Col 1  ',                   style='on #252526')
    status.append(f'  {progress}%  ',                          style='on #252526')
    if at_end:
        status.append('  END  ',       style='bold on #4ec9b0')
    if resumed:
        status.append('  ↩ resumed  ', style='bold on #1e4620')

    hint = Text()
    hint.append(
        '  j/↓ k/↑ scroll   d/u half-page   Space/b page   n/p chapter   t toc   w define   r reset   Esc panic   q quit',
        style='dim',
    )

    os.system('cls' if os.name == 'nt' else 'clear')
    console.print(tabs)

    if debug_mode:
        dbg = _debug_panel(chapter_idx, line_offset)
        tbl = Table.grid(padding=0, expand=True)
        tbl.add_column(ratio=64)
        tbl.add_column(ratio=36)
        tbl.add_row(syntax, dbg)
        console.print(tbl)
    else:
        console.print(syntax)

    if log_tick is not None:
        console.print(_log_panel(log_tick, term_w))

    console.print(status)
    if show_activity:
        console.print(_activity_text(_time.monotonic()))
    console.print(hint)

    return n_lines


def render_toc(book_title: str, chapters: List[dict], cursor: int) -> None:
    term_w, term_h = shutil.get_terminal_size((120, 40))
    fp, _ = fake_path(book_title, 'table_of_contents')

    max_visible = max(5, term_h - 12)
    total = len(chapters)

    if total <= max_visible:
        start, end = 0, total
    else:
        half  = max_visible // 2
        start = max(0, cursor - half)
        end   = min(total, start + max_visible)
        if end == total:
            start = max(0, total - max_visible)

    rows = []
    for i in range(start, end):
        marker = '# >>' if i == cursor else '#   '
        title  = chapters[i]['title'].replace('"', "'")[:65]
        rows.append(f'    {marker} {i + 1:02d}. {title}')

    scroll_note = f'  # {start + 1}–{end} of {total}' if total > max_visible else ''
    code = (
        f'# {fp}\n'
        f'# Chapter index{scroll_note}\n'
        f'\n'
        f'CHAPTERS = [\n'
        + ',\n'.join(rows) + ',\n'
        f']\n'
        f'\n'
        f'def get_chapter(n: int) -> str:\n'
        f'    return CHAPTERS[n]\n'
    )

    syntax = Syntax(code, 'python', theme='monokai', line_numbers=True)

    os.system('cls' if os.name == 'nt' else 'clear')
    console.print(syntax)
    hint = Text()
    hint.append('\n  j/k navigate   Enter to jump   Esc panic   q quit', style='dim')
    console.print(hint)


def render_definition(word: str, data) -> None:
    """Render a dictionary definition full-screen. Caller waits for keypress after."""
    os.system('cls' if os.name == 'nt' else 'clear')

    content = Text()

    if data is None:
        content.append('\n  No definition found for ', style='dim')
        content.append(f'"{word}"', style='white')
        content.append('.', style='dim')
        content.append('\n\n  Check your spelling or internet connection.\n', style='dim')
    else:
        content.append('\n')
        for meaning in data.get('meanings', []):
            pos = meaning.get('partOfSpeech', '')
            content.append(f'  {pos}\n', style='italic #888888')
            for defn in meaning.get('definitions', [])[:3]:
                content.append(f'\n  • ', style='dim')
                content.append(defn.get('definition', '') + '\n', style='white')
                if ex := defn.get('example'):
                    content.append(f'    "{ex}"\n', style='italic dim')
            content.append('\n')

    phonetic = ''
    if data:
        phonetic = next(
            (p.get('text', '') for p in data.get('phonetics', []) if p.get('text')), ''
        )

    title = f'[bold white]{word}[/bold white]'
    if phonetic:
        title += f'  [dim]{phonetic}[/dim]'

    console.print(Panel(content, title=title, border_style='dim white', box=box.ROUNDED))
    console.print('\n  [dim]press any key to continue reading...[/dim]')
