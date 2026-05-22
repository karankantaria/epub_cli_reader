"""Create a minimal test EPUB for smoke-testing reader.py."""
import zipfile

CHAPTER = b"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html><head><title>Chapter 1</title></head><body>
<h1>Chapter 1: The Beginning</h1>
<p>Call me Ishmael. Some years ago, never mind how long precisely, having little money
in my pocket and nothing particular to interest me on shore, I thought I would sail
about a little and see the watery part of the world.</p>
<p>It is a way I have of driving off the spleen and regulating the circulation.
Whenever I find myself growing grim about the mouth; whenever it is a damp, drizzly
November in my soul; whenever I find myself involuntarily pausing before coffin
warehouses, and bringing up the rear of every funeral I meet; and especially whenever
my hypos get such an upper hand of me, that it requires a strong moral principle to
prevent me from deliberately stepping into the street, and methodically knocking
people's hats off then, I account it high time to get to sea as soon as I can.</p>
<p>This is my substitute for pistol and ball. With a philosophical flourish Cato throws
himself upon his sword; I quietly take to the ship. There is nothing surprising in
this. If they only knew it, almost all men in their degree, some time or other, cherish
very nearly the same feelings towards the ocean with me.</p>
<h2>Chapter 2: The Carpet-Bag</h2>
<p>I stuffed a shirt or two into my old carpet-bag, tucked it under my arm, and started
for Cape Horn and the Pacific. Quitting the good city of old Manhatto, I duly arrived
in New Bedford. It was a Saturday night in December. Much was I disappointed upon
learning that the little packet for Nantucket had already sailed, and that no way of
reaching that place would offer itself until the following Monday.</p>
</body></html>"""

OPF = b"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Moby Dick (Test)</dc:title>
    <dc:creator>Herman Melville</dc:creator>
    <dc:identifier id="uid">test-moby-001</dc:identifier>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="ch1" href="chapter1.html" media-type="application/xhtml+xml"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="ch1"/>
  </spine>
</package>"""

NCX = b"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="test-moby-001"/></head>
  <docTitle><text>Moby Dick (Test)</text></docTitle>
  <navMap>
    <navPoint id="np1" playOrder="1">
      <navLabel><text>Chapter 1</text></navLabel>
      <content src="chapter1.html"/>
    </navPoint>
  </navMap>
</ncx>"""

CONTAINER = b"""<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

with zipfile.ZipFile('test.epub', 'w', zipfile.ZIP_DEFLATED) as z:
    z.writestr('mimetype', 'application/epub+zip')
    z.writestr('META-INF/container.xml', CONTAINER)
    z.writestr('content.opf', OPF)
    z.writestr('toc.ncx', NCX)
    z.writestr('chapter1.html', CHAPTER)

print('test.epub created OK')
