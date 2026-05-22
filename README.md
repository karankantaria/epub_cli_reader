# epub_cli_reader
Reads EPUB files in the terminal, disguised as syntax-highlighted Python code.

## Setup
```
pip install -r requirements.txt
```

## Usage
```
python reader.py book.epub
python reader.py book.epub --chapter 3
```

## Keys
| Key | Action |
|-----|--------|
| `j` / `↓` | Scroll down one line |
| `k` / `↑` | Scroll up one line |
| `d` / `u` | Half-page down / up |
| `Space` / `b` | Full page down / up |
| `n` / `p` | Next / previous chapter |
| `g` / `G` | Top / bottom of chapter |
| `t` | Table of contents |
| `q` | Quit |
