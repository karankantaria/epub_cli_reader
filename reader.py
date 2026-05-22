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
    q           quit
"""

import argparse
import os
import sys
import shutil
from typing import List

from epub_parser import load_epub
from renderer import render_page, render_toc, wrap_chapter, wrap_width, content_line_count


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
            return 'QUIT'
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
                return 'QUIT'
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
    parser.add_argument('-c', '--chapter', type=int, default=0, metavar='N',
                        help='Start at chapter N (0-indexed, default 0)')
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

    chapter_idx = max(0, min(args.chapter, len(chapters) - 1))
    line_offset = 0
    in_toc = False
    toc_cursor = chapter_idx

    # Cache wrapped text per (chapter_index, terminal_width)
    wrap_cache: dict = {}

    def get_wrapped(idx: int) -> List[str]:
        term_w, _ = shutil.get_terminal_size((120, 40))
        key = (idx, term_w)
        if key not in wrap_cache:
            wrap_cache[key] = wrap_chapter(chapters[idx]['content'], wrap_width(term_w))
        return wrap_cache[key]

    # Tracks how many content lines were shown last render (for page-scroll math)
    shown = 20

    while True:
        if in_toc:
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
            )

        try:
            key = get_key()
        except KeyboardInterrupt:
            key = 'QUIT'

        # ── TOC mode ──
        if in_toc:
            if key in ('q', 'QUIT', 't', '\x1b'):
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

        # ── Reader mode ──
        wrapped = get_wrapped(chapter_idx)
        total = len(wrapped)
        max_off = max(0, total - shown)

        if key in ('q', 'QUIT'):
            os.system('cls' if os.name == 'nt' else 'clear')
            break
        elif key in ('j', 'DOWN'):
            if line_offset >= max_off and chapter_idx < len(chapters) - 1:
                chapter_idx += 1
                line_offset = 0
                toc_cursor = chapter_idx
            else:
                line_offset = min(line_offset + 1, max_off)
        elif key in ('k', 'UP'):
            if line_offset == 0 and chapter_idx > 0:
                chapter_idx -= 1
                toc_cursor = chapter_idx
                wrapped = get_wrapped(chapter_idx)
                _, term_h = shutil.get_terminal_size((120, 40))
                line_offset = max(0, len(wrapped) - content_line_count(term_h))
            else:
                line_offset = max(0, line_offset - 1)
        elif key == 'd':
            line_offset = min(line_offset + shown // 2, max_off)
        elif key == 'u':
            line_offset = max(0, line_offset - shown // 2)
        elif key in ('f', ' ', 'PGDN'):
            if line_offset >= max_off and chapter_idx < len(chapters) - 1:
                chapter_idx += 1
                line_offset = 0
                toc_cursor = chapter_idx
            else:
                line_offset = min(line_offset + shown, max_off)
        elif key in ('b', 'PGUP'):
            line_offset = max(0, line_offset - shown)
        elif key == 'g':
            line_offset = 0
        elif key == 'G':
            line_offset = max_off
        elif key == 'n':
            if chapter_idx < len(chapters) - 1:
                chapter_idx += 1
                line_offset = 0
                toc_cursor = chapter_idx
        elif key == 'p':
            if chapter_idx > 0:
                chapter_idx -= 1
                line_offset = 0
                toc_cursor = chapter_idx
        elif key == 't':
            in_toc = True
            toc_cursor = chapter_idx


if __name__ == '__main__':
    main()
