"""EPUB -> chapters, with the standard library only.

Reads the OPF spine in order, splits each document at its `div.chapter` sections,
treats a heading like "PART ONE" as a part marker and any other heading followed by
paragraphs as a chapter. Figures, captions, and Project Gutenberg boilerplate are
dropped. Output is plain paragraphs with soft line breaks collapsed. Italics inside a
paragraph are kept as `*markers*`, as the Royal Road parser keeps them, and headings
stay plain.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

from .royalroad import close_italic, fix_italics

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
SKIP_IDS = {"pg-header", "pg-footer", "coverpage-wrapper"}
PART_RE = re.compile(r"^(PART|BOOK|VOLUME)\s+[\w-]+\.?$", re.IGNORECASE)
CHAPTER_RE = re.compile(r"^(CHAPTER|CHAP\.?)\s+([IVXLC]+|\d+)\.?$", re.IGNORECASE)
ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


@dataclass
class Section:
    heading_lines: list[str] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)


@dataclass
class Chapter:
    number: int
    part: int
    part_title: str
    label: str
    title: str
    paragraphs: list[str]
    source_file: str

    @property
    def text(self) -> str:
        head = f"# {self.label}: {self.title}" if self.title else f"# {self.label}"
        return head + "\n\n" + "\n\n".join(self.paragraphs) + "\n"


class _SectionParser(HTMLParser):
    """Collects headings and paragraphs per `div.chapter`; skips figures and boilerplate."""

    BLOCK = {"p", "blockquote", "li", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections: list[Section] = [Section()]
        self._skip_depth = 0
        self._div_depth = 0
        self._in_heading = False
        self._block: list[str] | None = None
        self._heading: list[str] = []
        self._italics = 0
        self._italic_opened = False
        self._marked = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "")
        if self._skip_depth:
            if tag == "div":
                self._skip_depth += 1
            return
        if tag in ("header", "footer") or (tag == "div" and ("fig" in cls.split() or "figcenter" in cls)):
            self._skip_depth = 1
            return
        if tag == "div" and "chapter" in cls.split():
            self.sections.append(Section())
        if tag in ("h1", "h2", "h3"):
            self._in_heading = True
            self._heading = [""]
        elif tag in self.BLOCK and "caption" not in cls:
            self._block = [""]
            self._italics, self._italic_opened, self._marked = 0, False, False
        elif tag in ("em", "i") and self._block is not None and not self._in_heading:
            self._block[-1] += "*"
            self._italics += 1
            self._italic_opened = self._marked = True
        elif tag == "br":
            if self._in_heading:
                self._heading.append("")
            elif self._block is not None:
                self._block[-1] += " "
        elif tag == "td" and self._block is not None:
            self._block[-1] += " "

    def handle_endtag(self, tag):
        if self._skip_depth:
            if tag in ("div", "header", "footer"):
                self._skip_depth -= 1
            return
        if tag in ("h1", "h2", "h3") and self._in_heading:
            self._in_heading = False
            lines = [_clean(x) for x in self._heading]
            self.sections[-1].heading_lines.extend(l for l in lines if l)
        elif tag in ("em", "i") and self._block is not None and self._italics:
            self._block[-1] = close_italic(self._block[-1])
            self._italics -= 1
        elif tag in self.BLOCK and self._block is not None:
            text = _clean(" ".join(self._block))
            text = fix_italics(text) if self._marked else text
            if text:
                self.sections[-1].paragraphs.append(text)
            self._block = None

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_heading:
            self._heading[-1] += data
        elif self._block is not None:
            if self._italic_opened:
                # '* word' -> ' *word': leading whitespace of the first italic text moves before the marker
                self._italic_opened = False
                lead = data[:len(data) - len(data.lstrip())]
                if lead and self._block[-1].endswith("*"):
                    self._block[-1] = self._block[-1][:-1] + lead + "*"
                    data = data.lstrip()
            self._block[-1] += data


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _roman(s: str) -> int:
    s = s.upper()
    if s.isdigit():
        return int(s)
    total = 0
    for i, ch in enumerate(s):
        v = ROMAN[ch]
        total += -v if i + 1 < len(s) and ROMAN[s[i + 1]] > v else v
    return total


def title_case(text: str) -> str:
    """ALL CAPS -> Title Case, keeping apostrophes inside words ("NED LAND’S" -> "Ned Land’s")."""
    if text != text.upper():
        return text
    return re.sub(r"\w+(?:[’']\w+)?", lambda m: m.group(0).capitalize(), text)


def extract(epub_path) -> tuple[dict, list[Chapter]]:
    with zipfile.ZipFile(epub_path) as zf:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        opf_path = container.find(".//c:rootfile", NS).get("full-path")
        opf_dir = posixpath.dirname(opf_path)
        opf = ET.fromstring(zf.read(opf_path))
        meta = {
            "title": (opf.findtext(".//dc:title", default="", namespaces=NS) or "").strip(),
            "author": (opf.findtext(".//dc:creator", default="", namespaces=NS) or "").strip(),
            "language": (opf.findtext(".//dc:language", default="", namespaces=NS) or "").strip(),
        }
        items = {}
        for item in opf.find("opf:manifest", NS):
            items[item.get("id")] = (item.get("href"), item.get("media-type"), item.get("properties", ""))
        docs = []
        for ref in opf.find("opf:spine", NS):
            idref = ref.get("idref")
            href, mtype, props = items[idref]
            if idref in SKIP_IDS or "nav" in props.split() or "xhtml" not in (mtype or ""):
                continue
            docs.append((href, zf.read(posixpath.join(opf_dir, href) if opf_dir else href).decode("utf-8")))

    chapters: list[Chapter] = []
    part, part_title = 0, ""
    for href, html in docs:
        parser = _SectionParser()
        parser.feed(html)
        for section in parser.sections:
            if not section.heading_lines:
                continue
            first = section.heading_lines[0]
            if PART_RE.match(first):
                part += 1
                part_title = title_case(" ".join(section.heading_lines))
                continue
            if not section.paragraphs:
                continue
            m = CHAPTER_RE.match(first)
            if m:
                label = f"Chapter {m.group(2).upper()}"
                title = title_case(" ".join(section.heading_lines[1:]))
            else:
                label = title_case(first)
                title = title_case(" ".join(section.heading_lines[1:]))
            chapters.append(Chapter(
                number=len(chapters) + 1, part=part, part_title=part_title, label=label, title=title,
                paragraphs=section.paragraphs, source_file=href,
            ))
    return meta, chapters


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
