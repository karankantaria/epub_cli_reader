import hashlib
import os
import re
import shutil
import textwrap
from typing import List, Tuple

from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text

console = Console()

# Lines in the fake-code "frame" that wrap the book content:
#   # filepath            (1)
#   # comment             (2)
#   <blank>               (3)
#   class Name:           (4)
#       """               (5)
#   --- content lines ---
#       """               (+1)
#   <blank>               (+2)
#       def __init__:     (+3)
#           body          (+4)
_HEADER_LINES = 5
_FOOTER_LINES = 4
_UI_LINES = 3   # tab bar + status bar + hint line
_OVERHEAD = _HEADER_LINES + _FOOTER_LINES + _UI_LINES  # 12


def content_line_count(term_h: int) -> int:
    return max(5, term_h - _OVERHEAD - 2)  # -2 buffer


def wrap_width(term_w: int) -> int:
    gutter = 8   # Rich line-number column
    indent = 4   # inside docstring
    return max(30, term_w - gutter - indent - 2)


# ── Fake path helpers ────────────────────────────────────────────────────────

def _slug(s: str) -> str:
    s = re.sub(r'[^\w\s]', '', s.lower())
    return re.sub(r'\s+', '_', s.strip())[:25] or 'module'


def fake_path(book_title: str, chapter_title: str) -> Tuple[str, str]:
    book_slug = _slug(book_title)
    ch_slug = _slug(chapter_title)
    dirs = ['src/core', 'src/utils', 'lib/analysis', 'src/modules', 'core/processing']
    idx = int(hashlib.md5(book_slug.encode()).hexdigest(), 16) % len(dirs)
    fp = f"{dirs[idx]}/{book_slug}/{ch_slug}.py"
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


# ── Renderers ────────────────────────────────────────────────────────────────

def render_page(
    book_title: str,
    chapter_title: str,
    wrapped: List[str],
    line_offset: int,
    chapter_idx: int,
    total_chapters: int,
) -> int:
    """Draw one page. Returns the number of content lines shown."""
    term_w, term_h = shutil.get_terminal_size((120, 40))
    n_lines = content_line_count(term_h)

    fp, fname = fake_path(book_title, chapter_title)
    cls = classname(chapter_title)
    base_ln = 280 + chapter_idx * 170

    page = wrapped[line_offset: line_offset + n_lines]
    while len(page) < n_lines:
        page.append('')

    # Escape triple-double-quotes so they don't break the syntax token
    safe = [ln.replace('"""', '"') for ln in page]
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

    syntax = Syntax(code, 'python', theme='monokai', line_numbers=True, start_line=base_ln)

    total = len(wrapped)
    progress = min(100, int(((line_offset + n_lines) / max(total, 1)) * 100))
    cur_ln = base_ln + _HEADER_LINES + line_offset
    at_end = line_offset + n_lines >= total

    # Tab bar
    tabs = Text()
    tabs.append('  main.py  ', style='dim on #2d2d2d')
    tabs.append('  config.py  ', style='dim on #2d2d2d')
    tabs.append(f'  {fname}  ', style='bold white on #1e1e1e')
    tabs.append('  utils.py  ', style='dim on #2d2d2d')
    tabs.append('  tests.py  ', style='dim on #2d2d2d')

    # Status bar
    status = Text()
    status.append('  ⎇ main  ', style='bold on #0e639c')
    status.append(f'  {fname}  ', style='on #37373d')
    status.append('  Python 3.11  ', style='on #252526')
    status.append('  UTF-8  ', style='on #252526')
    status.append(f'  Ch {chapter_idx + 1}/{total_chapters}  ', style='on #252526')
    status.append(f'  Ln {cur_ln}, Col 1  ', style='on #252526')
    status.append(f'  {progress}%  ', style='on #252526')
    if at_end:
        status.append('  END  ', style='bold on #4ec9b0')

    hint = Text()
    hint.append(
        '  j/↓ k/↑ scroll   d/u half-page   Space/b page   n/p chapter   t toc   q quit',
        style='dim',
    )

    os.system('cls' if os.name == 'nt' else 'clear')
    console.print(tabs)
    console.print(syntax)
    console.print(status)
    console.print(hint)

    return n_lines


def render_toc(book_title: str, chapters: List[dict], cursor: int) -> None:
    fp, _ = fake_path(book_title, 'table_of_contents')

    rows = []
    for i, ch in enumerate(chapters):
        marker = '# >>' if i == cursor else '#   '
        title = ch['title'].replace('"', "'")[:65]
        rows.append(f'    {marker} {i + 1:02d}. {title}')

    code = (
        f'# {fp}\n'
        f'# Chapter index\n'
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
    hint.append('\n  j/k navigate   Enter to jump   any key to return', style='dim')
    console.print(hint)
