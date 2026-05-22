"""
Persist reading position for each EPUB.

Progress is stored in ~/.epub_cli_progress.json, keyed by the absolute
path of the epub file. Line position is saved as a fraction (0.0–1.0) of
the chapter's wrapped lines so it survives terminal-width changes.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Tuple

PROGRESS_FILE = Path.home() / '.epub_cli_progress.json'


def _load_all() -> dict:
    try:
        with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_all(data: dict) -> None:
    try:
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass  # Never let a save failure interrupt reading


def load_progress(epub_path: str) -> Tuple[int, float]:
    """
    Return (chapter_idx, line_fraction) for this epub.
    line_fraction is 0.0–1.0 representing position within the chapter.
    Returns (0, 0.0) if no saved progress exists.
    """
    key = os.path.abspath(epub_path)
    entry = _load_all().get(key, {})
    return entry.get('chapter', 0), entry.get('line_fraction', 0.0)


def save_progress(
    epub_path: str,
    chapter_idx: int,
    line_fraction: float,
    book_title: str = '',
) -> None:
    """Persist the current position for this epub."""
    key = os.path.abspath(epub_path)
    data = _load_all()
    data[key] = {
        'chapter': chapter_idx,
        'line_fraction': round(line_fraction, 6),
        'title': book_title,
        'updated': datetime.now().isoformat(timespec='seconds'),
    }
    _save_all(data)


def clear_progress(epub_path: str) -> None:
    """Remove saved progress for this epub."""
    key = os.path.abspath(epub_path)
    data = _load_all()
    if key in data:
        del data[key]
        _save_all(data)
