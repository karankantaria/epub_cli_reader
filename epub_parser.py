import re
from typing import List, Tuple

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup


def _html_to_text(html_bytes: bytes) -> str:
    soup = BeautifulSoup(html_bytes, 'html.parser')

    for tag in soup(['script', 'style', 'head', 'nav', 'aside']):
        tag.decompose()

    for tag in soup.find_all(['p', 'div', 'br', 'li']):
        tag.insert_after('\n\n')
    for tag in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        tag.insert_before('\n\n')
        tag.insert_after('\n\n')

    text = soup.get_text(separator=' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = '\n'.join(line.strip() for line in text.splitlines())
    return text.strip()


def load_epub(filepath: str) -> Tuple[str, str, List[dict]]:
    """Return (title, author, chapters) from an EPUB file."""
    book = epub.read_epub(filepath)

    title_meta = book.get_metadata('DC', 'title')
    author_meta = book.get_metadata('DC', 'creator')
    title = title_meta[0][0] if title_meta else 'Unknown Title'
    author = author_meta[0][0] if author_meta else 'Unknown Author'

    chapters = []

    # Walk spine for reading order
    for item_id, _ in book.spine:
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue

        content_bytes = item.get_content()
        text = _html_to_text(content_bytes)
        if len(text.strip()) < 80:
            continue

        soup = BeautifulSoup(content_bytes, 'html.parser')
        heading = soup.find(['h1', 'h2', 'h3', 'h4'])
        if heading:
            chapter_title = heading.get_text(separator=' ').strip()
        else:
            name = item.get_name()
            chapter_title = re.sub(r'\.(x?html?)$', '', name.split('/')[-1], flags=re.I)

        chapters.append({'title': chapter_title, 'content': text})

    # Fallback if spine gave nothing useful
    if not chapters:
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            text = _html_to_text(item.get_content())
            if len(text.strip()) >= 80:
                name = item.get_name()
                title_fb = re.sub(r'\.(x?html?)$', '', name.split('/')[-1], flags=re.I)
                chapters.append({'title': title_fb, 'content': text})

    return title, author, chapters
