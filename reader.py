#!/usr/bin/env python3
"""
epub-reader — Read EPUB files in the terminal, disguised as code.

Usage:
    python reader.py book.epub
    python reader.py book.epub --chapter 3

Keys:
    j / ↓       scroll down one line
    k / ↑       scroll up one line
    d / u       half-page down / up
    Space / b   full page down / up
    n / p       next / previous chapter
    g / G       top / bottom of chapter
    t           table of contents
    r           reset position (forget saved place)
    Esc         toggle panic view (fake Python module)
    q           quit
"""

import argparse
import os
import sys
import shutil
from typing import List

from epub_parser import load_epub
from panic import render_panic, FAKE_CODE
from progress import load_progress, save_progress, clear_progress
from renderer import render_page, render_toc, wrap_chapter, wrap_width, content_line_count

# How many navigation steps between autosaves
_AUTOSAVE_INTERVAL = 20


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
                        sys.stdin.read(1)  # consume trailing '~'
                    return {'A': 'UP', 'B': 'DOWN', 'C': 'RIGHT', 'D': 'LEFT',
                            '5': 'PGUP', '6': 'PGDN'}.get(code, 'UNKNOWN')
                return 'ESC'
            if ch in ('\x03', '\x04'):
                return 'QUIT'
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


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
    args = parser.parse_args()

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
        # Explicit --chapter flag overrides saved progress
        chapter_idx = max(0, min(args.chapter, len(chapters) - 1))
        saved_fraction = 0.0
        resumed = False
    else:
        saved_chapter, saved_fraction = load_progress(args.epub_file)
        chapter_idx = max(0, min(saved_chapter, len(chapters) - 1))
        resumed = saved_chapter > 0 or saved_fraction > 0.0

    # line_offset within the chapter is resolved after the first wrap (below)
    line_offset = 0
    in_toc   = False
    in_panic = False
    toc_cursor    = chapter_idx
    panic_offset  = 0
    panic_shown   = 20
    _PANIC_TOTAL  = len(FAKE_CODE.splitlines())

    # Cache wrapped text per (chapter_index, terminal_width)
    wrap_cache: dict = {}

    def get_wrapped(idx: int) -> List[str]:
        term_w, _ = shutil.get_terminal_size((120, 40))
        cache_key = (idx, term_w)
        if cache_key not in wrap_cache:
            wrap_cache[cache_key] = wrap_chapter(chapters[idx]['content'], wrap_width(term_w))
        return wrap_cache[cache_key]

    def current_fraction() -> float:
        """Position within the current chapter as a 0.0–1.0 fraction."""
        total = len(get_wrapped(chapter_idx))
        return line_offset / max(total - 1, 1)

    def do_save() -> None:
        save_progress(args.epub_file, chapter_idx, current_fraction(), book_title)

    # Resolve saved fraction → line_offset for the starting chapter
    wrapped = get_wrapped(chapter_idx)
    if resumed:
        line_offset = round(saved_fraction * max(len(wrapped) - 1, 1))
        # Clamp to valid range in case the epub changed since last read
        max_off_start = max(0, len(wrapped) - content_line_count(shutil.get_terminal_size((120, 40))[1]))
        line_offset = min(line_offset, max_off_start)

    # Tracks how many content lines were shown last render (for page-scroll math)
    shown = 20
    # Autosave counter: save every _AUTOSAVE_INTERVAL navigation steps
    nav_since_save = 0

    while True:
        # ── Render ────────────────────────────────────────────────────
        if in_panic:
            panic_shown = render_panic(panic_offset)
        elif in_toc:
            render_toc(book_title, chapters, toc_cursor)
        else:
            wrapped = get_wrapped(chapter_idx)
            shown = render_page(
                book_title,
                chapters[chapter_idx]['title'],
                wrapped,
                line_offset,
                chapter_idx,
                len(chapters),
                resumed=resumed,
            )
            resumed = False  # ↩ badge shows only on the first render after loading

        try:
            key = get_key()
        except KeyboardInterrupt:
            key = 'QUIT'

        # ── Panic mode (Escape toggles; q still quits) ────────────────
        if in_panic:
            max_panic = max(0, _PANIC_TOTAL - panic_shown)
            if key in ('q', 'QUIT'):
                do_save()
                os.system('cls' if os.name == 'nt' else 'clear')
                return
            elif key in ('ESC',):
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
                in_toc = False
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
            """Apply a navigation step and tick the autosave counter."""
            nonlocal chapter_idx, line_offset, toc_cursor, nav_since_save
            if delta_chapter:
                chapter_idx += delta_chapter
                chapter_idx = max(0, min(chapter_idx, len(chapters) - 1))
                line_offset = 0
                toc_cursor  = chapter_idx
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
            if line_offset >= max_off and chapter_idx < len(chapters) - 1:
                nav(delta_chapter=+1)
            else:
                nav(new_offset=line_offset + 1)

        elif key in ('k', 'UP'):
            if line_offset == 0 and chapter_idx > 0:
                nav(delta_chapter=-1)
                prev_wrapped = get_wrapped(chapter_idx)
                _, term_h = shutil.get_terminal_size((120, 40))
                line_offset = max(0, len(prev_wrapped) - content_line_count(term_h))
            else:
                nav(new_offset=line_offset - 1)

        elif key == 'd':
            nav(new_offset=line_offset + shown // 2)

        elif key == 'u':
            nav(new_offset=line_offset - shown // 2)

        elif key in ('f', ' ', 'PGDN'):
            if line_offset >= max_off and chapter_idx < len(chapters) - 1:
                nav(delta_chapter=+1)
            else:
                nav(new_offset=line_offset + shown)

        elif key in ('b', 'PGUP'):
            nav(new_offset=line_offset - shown)

        elif key == 'g':
            nav(new_offset=0)

        elif key == 'G':
            nav(new_offset=max_off)

        elif key == 'n':
            if chapter_idx < len(chapters) - 1:
                nav(delta_chapter=+1)

        elif key == 'p':
            if chapter_idx > 0:
                nav(delta_chapter=-1)

        elif key == 't':
            in_toc = True
            toc_cursor = chapter_idx

        elif key == 'r':
            clear_progress(args.epub_file)
            chapter_idx = 0
            line_offset = 0
            toc_cursor  = 0
            nav_since_save = 0


if __name__ == '__main__':
    main()
