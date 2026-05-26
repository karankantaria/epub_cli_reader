#!/usr/bin/env python3
"""
epub-reader — Read EPUB files in the terminal, disguised as code.

Usage:
    python reader.py book.epub
    python reader.py book.epub --chapter 3
    python reader.py book.epub --high
    python reader.py book.epub --diff
    python reader.py book.epub --debug
    python reader.py book.epub --drift --log --activity

Rendering modes (mutually exclusive — pick at most one):
    --high        Prose as green comments, code expressions between paragraphs
    --diff        Content disguised as a git diff review
    --debug       Content with a fake debug variable panel on the right

Ambient overlays (freely stackable with each other and any rendering mode):
    --drift       Auto-scroll when idle (45 s grace, then 1 line every 3 s)
    --log         Ambient server log panel at the bottom
    --activity    Pulsing activity: indexing spinner, test runner, build progress

Keys:
    j / ↓       scroll down one line
    k / ↑       scroll up one line
    d / u       half-page down / up
    Space / b   full page down / up
    n / p       next / previous chapter
    g / G       top / bottom of chapter
    t           table of contents
    w           look up a word in the dictionary
    r           reset position (forget saved place)
    Esc         toggle panic view (fake Python module)
    q           quit
"""

import argparse
import json
import os
import sys
import shutil
import time
import urllib.request
from typing import List

from epub_parser import load_epub
from panic import render_panic, FAKE_CODE
from progress import load_progress, save_progress, clear_progress
from renderer import (
    render_page, render_toc, render_definition,
    wrap_chapter, wrap_width, content_line_count, mode_overhead,
)

_AUTOSAVE_INTERVAL = 20


def _fetch_definition(word: str):
    try:
        url = f'https://api.dictionaryapi.dev/api/v2/entries/en/{word.lower().strip()}'
        req = urllib.request.Request(url, headers={'User-Agent': 'onyx-reader/1.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())[0]
    except Exception:
        return None


# ── Keyboard input ────────────────────────────────────────────────────────────

def get_key() -> str:
    if os.name == 'nt':
        import msvcrt
        b = msvcrt.getch()
        if b in (b'\x00', b'\xe0'):
            b2 = msvcrt.getch()
            return {
                b'H': 'UP',   b'P': 'DOWN',
                b'K': 'LEFT', b'M': 'RIGHT',
                b'I': 'PGUP', b'Q': 'PGDN',
                b'G': 'HOME', b'O': 'END',
            }.get(b2, 'UNKNOWN')
        if b == b'\x03':
            return 'QUIT'
        if b == b'\x1b':
            return 'ESC'
        try:
            return b.decode('utf-8')
        except Exception:
            return ''
    else:
        import tty
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                nxt = sys.stdin.read(1)
                if nxt == '[':
                    code = sys.stdin.read(1)
                    if code in ('5', '6'):
                        sys.stdin.read(1)
                    return {'A': 'UP', 'B': 'DOWN', 'C': 'RIGHT', 'D': 'LEFT',
                            '5': 'PGUP', '6': 'PGDN'}.get(code, 'UNKNOWN')
                return 'ESC'
            if ch in ('\x03', '\x04'):
                return 'QUIT'
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def get_key_timed(timeout: float = 0.25) -> str:
    """Return next key or '' after timeout seconds with no keypress."""
    if os.name == 'nt':
        import msvcrt
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                return get_key()
            time.sleep(0.04)
        return ''
    else:
        import select
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        return get_key() if r else ''


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Read an EPUB in the terminal.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('epub_file', help='Path to the .epub file')
    parser.add_argument('-c', '--chapter', type=int, default=None, metavar='N',
                        help='Start at chapter N (0-indexed); overrides saved position')
    # Rendering modes
    parser.add_argument('--high',     action='store_true',
                        help='Stealth mode: prose as comments, code between paragraphs')
    parser.add_argument('--diff',     action='store_true',
                        help='Disguise content as a git diff review')
    parser.add_argument('--debug',    action='store_true',
                        help='Show content alongside a fake debug variable panel')
    # Ambient overlays
    parser.add_argument('--drift',    action='store_true',
                        help='Auto-scroll when idle (45 s grace, then 1 line/3 s)')
    parser.add_argument('--log',      action='store_true',
                        help='Ambient server log panel at the bottom')
    parser.add_argument('--activity', action='store_true',
                        help='Pulsing activity: indexing, tests, builds, Copilot')
    args = parser.parse_args()

    if sum([args.high, args.diff, args.debug]) > 1:
        parser.error('--high, --diff, and --debug are mutually exclusive')

    try:
        print('Loading...', end='\r', flush=True)
        book_title, author, chapters = load_epub(args.epub_file)
    except FileNotFoundError:
        print(f'File not found: {args.epub_file}')
        sys.exit(1)
    except Exception as exc:
        print(f'Error loading EPUB: {exc}')
        sys.exit(1)

    if not chapters:
        print('No readable content found in this EPUB.')
        sys.exit(1)

    # ── Restore or set starting position ──
    if args.chapter is not None:
        chapter_idx    = max(0, min(args.chapter, len(chapters) - 1))
        saved_fraction = 0.0
        resumed        = False
    else:
        saved_chapter, saved_fraction = load_progress(args.epub_file)
        chapter_idx = max(0, min(saved_chapter, len(chapters) - 1))
        resumed     = saved_chapter > 0 or saved_fraction > 0.0

    line_offset  = 0
    in_toc       = False
    in_panic     = False
    toc_cursor   = chapter_idx
    panic_offset = 0
    panic_shown  = 20
    _PANIC_TOTAL = len(FAKE_CODE.splitlines())

    wrap_cache: dict = {}
    _mode = 'diff' if args.diff else 'debug' if args.debug else 'normal'

    def get_wrapped(idx: int) -> List[str]:
        term_w, _ = shutil.get_terminal_size((120, 40))
        cache_key  = (idx, term_w, _mode)
        if cache_key not in wrap_cache:
            wrap_cache[cache_key] = wrap_chapter(chapters[idx]['content'], wrap_width(term_w, _mode))
        return wrap_cache[cache_key]

    def current_fraction() -> float:
        total = len(get_wrapped(chapter_idx))
        return line_offset / max(total - 1, 1)

    def do_save() -> None:
        save_progress(args.epub_file, chapter_idx, current_fraction(), book_title)

    # Resolve saved fraction → line_offset for the starting chapter
    wrapped = get_wrapped(chapter_idx)
    if resumed:
        _extra      = mode_overhead(log=args.log, activity=args.activity)
        line_offset = round(saved_fraction * max(len(wrapped) - 1, 1))
        max_off_start = max(0, len(wrapped) - content_line_count(shutil.get_terminal_size((120, 40))[1], _extra))
        line_offset = min(line_offset, max_off_start)

    shown              = 20
    nav_since_save     = 0
    at_chapter_end     = False
    log_tick           = 0
    _last_activity_time = time.monotonic()
    _last_drift_time    = 0.0
    _needs_animation   = args.drift or args.log or args.activity or args.debug

    while True:
        # ── Drift auto-scroll ──────────────────────────────────────────
        if args.drift and not in_panic and not in_toc:
            now = time.monotonic()
            if now - _last_activity_time > 45.0 and now - _last_drift_time > 3.0:
                _wr  = get_wrapped(chapter_idx)
                _max = max(0, len(_wr) - shown)
                if line_offset < _max:
                    line_offset      += 1
                    _last_drift_time  = now
                elif chapter_idx < len(chapters) - 1:
                    chapter_idx      += 1
                    line_offset       = 0
                    toc_cursor        = chapter_idx
                    _last_drift_time  = now

        # ── Render ────────────────────────────────────────────────────
        if in_panic:
            panic_shown = render_panic(panic_offset)
        elif in_toc:
            render_toc(book_title, chapters, toc_cursor)
        else:
            wrapped = get_wrapped(chapter_idx)
            if args.log:
                log_tick += 1
            shown = render_page(
                book_title,
                chapters[chapter_idx]['title'],
                wrapped,
                line_offset,
                chapter_idx,
                len(chapters),
                resumed=resumed,
                high_mode=args.high,
                diff_mode=args.diff,
                debug_mode=args.debug,
                log_tick=log_tick if args.log else None,
                show_activity=args.activity,
            )
            resumed = False

        try:
            key = get_key_timed(0.25) if _needs_animation else get_key()
        except KeyboardInterrupt:
            key = 'QUIT'

        if key:
            _last_activity_time = time.monotonic()

        if not key:
            continue

        # ── Panic mode ────────────────────────────────────────────────
        if in_panic:
            max_panic = max(0, _PANIC_TOTAL - panic_shown)
            if key in ('q', 'QUIT'):
                do_save()
                os.system('cls' if os.name == 'nt' else 'clear')
                return
            elif key == 'ESC':
                in_panic = False
            elif key in ('j', 'DOWN'):
                panic_offset = min(panic_offset + 1, max_panic)
            elif key in ('k', 'UP'):
                panic_offset = max(0, panic_offset - 1)
            elif key == 'd':
                panic_offset = min(panic_offset + panic_shown // 2, max_panic)
            elif key == 'u':
                panic_offset = max(0, panic_offset - panic_shown // 2)
            elif key in ('f', ' ', 'PGDN'):
                panic_offset = min(panic_offset + panic_shown, max_panic)
            elif key in ('b', 'PGUP'):
                panic_offset = max(0, panic_offset - panic_shown)
            elif key == 'g':
                panic_offset = 0
            elif key == 'G':
                panic_offset = max_panic
            continue

        # ── ESC from anywhere else → enter panic ──────────────────────
        if key == 'ESC':
            in_panic = True
            in_toc   = False
            continue

        # ── TOC mode ──────────────────────────────────────────────────
        if in_toc:
            if key in ('q', 'QUIT', 't'):
                in_toc = False
            elif key in ('j', 'DOWN'):
                toc_cursor = min(toc_cursor + 1, len(chapters) - 1)
            elif key in ('k', 'UP'):
                toc_cursor = max(toc_cursor - 1, 0)
            elif key in ('\r', '\n', 'l', 'n', ' '):
                in_toc      = False
                chapter_idx = toc_cursor
                line_offset = 0
            else:
                in_toc = False
            continue

        # ── Reader mode ───────────────────────────────────────────────
        wrapped = get_wrapped(chapter_idx)
        total   = len(wrapped)
        max_off = max(0, total - shown)

        def nav(delta_chapter: int = 0, new_offset: int = None) -> None:
            nonlocal chapter_idx, line_offset, toc_cursor, nav_since_save
            if delta_chapter:
                chapter_idx += delta_chapter
                chapter_idx  = max(0, min(chapter_idx, len(chapters) - 1))
                line_offset  = 0
                toc_cursor   = chapter_idx
            elif new_offset is not None:
                line_offset = max(0, min(new_offset, max_off))
            nav_since_save += 1
            if nav_since_save >= _AUTOSAVE_INTERVAL:
                do_save()
                nav_since_save = 0

        if key in ('q', 'QUIT'):
            do_save()
            os.system('cls' if os.name == 'nt' else 'clear')
            break

        elif key in ('j', 'DOWN'):
            if line_offset >= max_off:
                if at_chapter_end and chapter_idx < len(chapters) - 1:
                    nav(delta_chapter=+1)
                    at_chapter_end = False
                else:
                    at_chapter_end = True
            else:
                at_chapter_end = False
                nav(new_offset=line_offset + 1)

        elif key in ('k', 'UP'):
            at_chapter_end = False
            if line_offset == 0 and chapter_idx > 0:
                nav(delta_chapter=-1)
                prev_wrapped = get_wrapped(chapter_idx)
                _, term_h    = shutil.get_terminal_size((120, 40))
                _extra       = mode_overhead(log=args.log, activity=args.activity)
                line_offset  = max(0, len(prev_wrapped) - content_line_count(term_h, _extra))
            else:
                nav(new_offset=line_offset - 1)

        elif key == 'd':
            at_chapter_end = False
            nav(new_offset=line_offset + shown // 2)

        elif key == 'u':
            at_chapter_end = False
            nav(new_offset=line_offset - shown // 2)

        elif key in ('f', ' ', 'PGDN'):
            if line_offset >= max_off:
                if at_chapter_end and chapter_idx < len(chapters) - 1:
                    nav(delta_chapter=+1)
                    at_chapter_end = False
                else:
                    at_chapter_end = True
            else:
                at_chapter_end = False
                nav(new_offset=line_offset + shown)

        elif key in ('b', 'PGUP'):
            at_chapter_end = False
            nav(new_offset=line_offset - shown)

        elif key == 'g':
            at_chapter_end = False
            nav(new_offset=0)

        elif key == 'G':
            at_chapter_end = False
            nav(new_offset=max_off)

        elif key == 'n':
            at_chapter_end = False
            if chapter_idx < len(chapters) - 1:
                nav(delta_chapter=+1)

        elif key == 'p':
            at_chapter_end = False
            if chapter_idx > 0:
                nav(delta_chapter=-1)

        elif key == 't':
            at_chapter_end = False
            in_toc     = True
            toc_cursor = chapter_idx

        elif key == 'w':
            os.system('cls' if os.name == 'nt' else 'clear')
            print('\n  ◆ dictionary\n')
            try:
                word = input('  word: ').strip()
            except (EOFError, KeyboardInterrupt):
                word = ''
            if word:
                print('\n  looking up...', end='\r', flush=True)
                data = _fetch_definition(word)
                render_definition(word, data)
                get_key()

        elif key == 'r':
            os.system('cls' if os.name == 'nt' else 'clear')
            print('\n  reset all progress? press r to confirm, any other key to cancel')
            if get_key() == 'r':
                clear_progress(args.epub_file)
                chapter_idx    = 0
                line_offset    = 0
                toc_cursor     = 0
                nav_since_save = 0


if __name__ == '__main__':
    main()
