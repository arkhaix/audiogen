"""Royal Road -> chapters, with the standard library only.

The fiction page carries the chapter index as `window.chapters = [...]`. Each chapter page holds the
text in `div.chapter-inner.chapter-content`; a decoy paragraph ("You could be reading stolen content")
is hidden by a per-page generated class declared `display: none` in a `<style>` block and is dropped
here, as is anything else styled hidden (spoiler bodies). The author's notes live outside the text in
`div.author-note-portlet` and are kept separately so they never reach the narrator.

Text comes out as Markdown-ish blocks: plain paragraphs, `> ` quoted paragraphs for blockquotes (LitRPG
system messages, letters, songs), `* * *` for a horizontal rule, and table rows joined with ` | `.
"""
from __future__ import annotations

import gzip
import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser

BASE = "https://www.royalroad.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")
MIN_INTERVAL = 2.0          # seconds between requests: this is one reader, not a crawler
FICTION_RE = re.compile(r"royalroad\.com/fiction/(\d+)(?:/([^/?#]+))?")
HIDDEN_RE = re.compile(r"\.([\w-]+)\s*\{[^}]*display\s*:\s*none", re.S)
TITLE_RE = re.compile(r"^\s*((?:chapter|ch\.?|interlude|epilogue|prologue)\s*[\w.]*)\s*[:.\-–—|]\s*(.+?)\s*$",
                      re.IGNORECASE)
_last_request = 0.0


class RoyalRoadError(RuntimeError):
    pass


def fetch(url: str, *, timeout: float = 60) -> str:
    """GET a page politely (one request every MIN_INTERVAL seconds, browser user agent, gzip)."""
    global _last_request
    wait = MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html",
                                               "Accept-Encoding": "gzip", "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
    finally:
        _last_request = time.monotonic()
    return data.decode("utf-8", errors="replace")


def fiction_id(url_or_id: str) -> int:
    if url_or_id.isdigit():
        return int(url_or_id)
    m = FICTION_RE.search(url_or_id)
    if not m:
        raise RoyalRoadError(f"not a Royal Road fiction URL: {url_or_id}")
    return int(m.group(1))


def _meta(html: str, prop: str) -> str:
    m = re.search(r'<meta\s+(?:property|name)="%s"\s+content="([^"]*)"' % re.escape(prop), html)
    return _unescape(m.group(1)).strip() if m else ""


def _unescape(text: str) -> str:
    import html as _html
    return _html.unescape(text)


def _strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>|</p>", "\n", fragment)
    return re.sub(r"\n{3,}", "\n\n", _unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def fiction(url_or_id: str) -> dict:
    """Title, author, blurb, volumes, and the full chapter index of a fiction."""
    fid = fiction_id(url_or_id)
    html = fetch(f"{BASE}/fiction/{fid}")
    m = re.search(r"window\.chapters\s*=\s*(\[.*?\]);", html, re.S)
    if not m:
        raise RoyalRoadError(f"fiction {fid}: no chapter index on the page (private, removed, or blocked?)")
    raw_chapters = json.loads(m.group(1))
    vm = re.search(r"window\.volumes\s*=\s*(\[.*?\]);", html, re.S)
    volumes = [{"id": v["id"], "order": v.get("order", i + 1), "title": v.get("title", "")}
               for i, v in enumerate(json.loads(vm.group(1)))] if vm else []
    vol_by_id = {v["id"]: v for v in volumes}

    full_title = _meta(html, "og:title") or _meta(html, "twitter:title")
    if not full_title:
        t = re.search(r"<title>(.*?)</title>", html, re.S)
        full_title = _unescape(t.group(1)).split("|")[0].strip() if t else f"Royal Road fiction {fid}"
    title, _, subtitle = full_title.partition(" [")
    subtitle = subtitle.rstrip("]").strip()
    author = _meta(html, "twitter:creator")
    if not author:
        a = re.search(r'href="/profile/\d+"[^>]*>([^<]+)<', html)
        author = _unescape(a.group(1)).strip() if a else ""
    desc = re.search(r'<div class="description">(.*?)</div>\s*</div>', html, re.S)
    tags = [_unescape(t).strip() for t in re.findall(r'class="[^"]*\bfiction-tag\b[^"]*"[^>]*>([^<]*)', html)]
    slug_m = re.search(r'property="og:url"\s+content="[^"]*/fiction/\d+/([^"/]+)"', html)
    canonical = f"{BASE}/fiction/{fid}" + (f"/{slug_m.group(1)}" if slug_m else "")

    chapters = []
    for i, c in enumerate(sorted(raw_chapters, key=lambda c: c.get("order", 0))):
        vol = vol_by_id.get(c.get("volumeId")) or {}
        chapters.append({
            "number": i + 1, "id": c["id"], "title": _unescape(c["title"]).strip(),
            "url": BASE + c["url"], "date": c.get("date", ""),
            "volume": vol.get("order", 0), "volume_title": vol.get("title", ""),
        })
    return {
        "fiction_id": fid, "url": canonical, "title": title.strip(), "subtitle": subtitle,
        "full_title": full_title, "author": author, "description": _strip_tags(desc.group(1)) if desc else "",
        "tags": tags, "volumes": volumes, "chapters": chapters,
    }


@dataclass
class ChapterText:
    title: str
    blocks: list[str] = field(default_factory=list)
    author_notes: list[str] = field(default_factory=list)

    @property
    def body(self) -> str:
        return "\n\n".join(self.blocks) + "\n"


class _ChapterParser(HTMLParser):
    """Collects text blocks from the chapter-content div and the author's notes around it."""

    BLOCK = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre"}

    def __init__(self, hidden_classes: set[str]):
        super().__init__(convert_charrefs=True)
        self.hidden = hidden_classes
        self.title = ""
        self.content: list[str] = []          # blocks of the chapter text
        self.notes: list[list[str]] = []      # one list of blocks per author's note
        self._stack: list[tuple[str, str]] = []   # (tag, mode) per open element; mode: "", "skip"
        self._target: list[str] | None = None     # where blocks go: content or a note
        self._target_depth = 0
        self._block: list[str] | None = None
        self._loose = ""                          # text outside any block element
        self._quote = 0
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self._in_title = False
        self._italic_opened = False

    # -- helpers
    def _skipping(self) -> bool:
        return any(mode == "skip" for _, mode in self._stack)

    def _flush_loose(self):
        for para in re.split(r"\n\s*\n", self._loose):
            para = _clean(para)
            if para:
                self._emit(para)
        self._loose = ""

    def _emit(self, text: str):
        if self._target is None:
            return
        if self._quote:
            text = "> " + text.replace("\n", "\n> ")
        self._target.append(text)

    # -- parser callbacks
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        style = (a.get("style") or "").replace(" ", "")
        mode = "skip" if (classes & self.hidden or "display:none" in style or tag in ("script", "style")) else ""
        self._stack.append((tag, mode))
        if tag == "h1" and self._target is None and "font-white" in classes and not self.title:
            self._in_title = True
            self._block = [""]
            return
        if self._target is None:
            if tag == "div" and {"chapter-inner", "chapter-content"} <= classes:
                self._target, self._target_depth = self.content, len(self._stack)
            elif tag == "div" and "author-note" in classes:
                self.notes.append([])
                self._target, self._target_depth = self.notes[-1], len(self._stack)
            return
        if mode == "skip" or self._skipping():
            return
        if tag in self.BLOCK:
            self._flush_loose()
            self._block = [""]
        elif tag == "blockquote":
            self._flush_loose()
            self._quote += 1
        elif tag == "hr":
            self._flush_loose()
            self._emit("* * *")
        elif tag == "table":
            self._flush_loose()
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = [""]
        elif tag in ("em", "i"):
            # italics are kept as *markers*: the screenplay pass uses them to spot unquoted thoughts
            if self._block is not None:
                self._block[-1] += "*"
                self._italic_opened = True
            elif self._cell is None:
                self._loose += "*"
                self._italic_opened = True
        elif tag == "br":
            if self._block is not None:
                self._block.append("")
            elif self._cell is not None:
                self._cell[-1] += " "
            else:
                self._loose += "\n"
        elif tag == "img" and a.get("alt"):
            self._loose += f"\n\n[image: {a['alt'].strip()}]\n\n"

    def handle_endtag(self, tag):
        # pop to the matching open tag (tolerates stray closers)
        if not any(t == tag for t, _ in self._stack):
            return
        while self._stack:
            t, _ = self._stack.pop()
            if t == tag:
                break
        if self._in_title and tag == "h1":
            self.title = _clean(" ".join(self._block or []))
            self._in_title = False
            self._block = None
            return
        if self._target is not None and len(self._stack) < self._target_depth:
            self._flush_loose()
            self._block = None
            self._quote = 0
            self._target = None
            return
        if self._target is None or self._skipping():
            return
        if tag in ("em", "i"):
            target = self._block if self._block is not None else (None if self._cell is not None else "loose")
            if target == "loose":
                self._loose = close_italic(self._loose)
            elif target is not None:
                target[-1] = close_italic(target[-1])
        elif tag in self.BLOCK and self._block is not None:
            lines = [fix_italics(_clean(x)) for x in self._block]
            text = "\n".join(l for l in lines if l)
            if text:
                self._emit(text)
            self._block = None
        elif tag == "blockquote":
            self._flush_loose()
            self._quote = max(0, self._quote - 1)
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(_clean(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            cells = [c for c in self._row if c]
            if cells:
                self._emit(" | ".join(cells))
            self._row = None

    def handle_data(self, data):
        if self._in_title:
            self._block[-1] += data
            return
        if self._target is None or self._skipping():
            return
        if self._italic_opened:
            # '* word' -> ' *word': leading whitespace of the first italic text moves before the marker
            self._italic_opened = False
            lead = data[:len(data) - len(data.lstrip())]
            if lead:
                if self._block is not None and self._block[-1].endswith("*"):
                    self._block[-1] = self._block[-1][:-1] + lead + "*"
                elif self._block is None and self._loose.endswith("*"):
                    self._loose = self._loose[:-1] + lead + "*"
                data = data.lstrip()
        if self._block is not None:
            self._block[-1] += data
        elif self._cell is not None:
            self._cell[-1] += data
        else:
            self._loose += data


def _clean(text: str) -> str:
    return re.sub(r"[ \t\r\f\v ]+", " ", text).strip()


def close_italic(text: str) -> str:
    """Put the closing marker before any trailing whitespace: '*very ' -> '*very* '."""
    stripped = text.rstrip()
    return stripped + "*" + text[len(stripped):]


def fix_italics(text: str) -> str:
    """Returns one line of text with its empty italic runs taken out, back-to-back runs joined, and the markers of a
    run that holds only punctuation dropped."""
    text = re.sub(r"\*\*", "", text)              # empty or back-to-back runs
    text = re.sub(r"\*([^\w*]*)\*", r"\1", text)  # italics holding only punctuation
    return _clean(text)


def parse_chapter(html: str) -> ChapterText:
    hidden = set()
    for style in re.findall(r"<style[^>]*>(.*?)</style>", html, re.S):
        hidden.update(HIDDEN_RE.findall(style))
    p = _ChapterParser(hidden)
    p.feed(html)
    p.close()
    notes = ["\n\n".join(n) for n in p.notes if any(b.strip() for b in n)]
    return ChapterText(title=p.title, blocks=p.content, author_notes=notes)


def split_title(raw: str, number: int) -> tuple[str, str]:
    """'Chapter 3: The One Act Play (1)' -> ('Chapter 3', 'The One Act Play (1)')."""
    m = TITLE_RE.match(raw)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return f"Chapter {number}", raw.strip()


@dataclass
class Chapter:
    number: int
    part: int
    part_title: str
    label: str
    title: str
    blocks: list[str]
    source_id: int
    source_url: str
    published: str
    author_notes: list[str]

    @property
    def text(self) -> str:
        head = f"# {self.label}: {self.title}" if self.title else f"# {self.label}"
        return head + "\n\n" + "\n\n".join(self.blocks) + "\n"


def chapter(entry: dict, html: str | None = None) -> Chapter:
    """Parse one chapter from an index entry as `fiction()` returns it, from `html` when given, else fetched."""
    parsed = parse_chapter(html if html is not None else fetch(entry["url"]))
    if not parsed.blocks:
        raise RoyalRoadError(f"{entry['url']}: no chapter text found")
    label, title = split_title(parsed.title or entry["title"], entry["number"])
    return Chapter(number=entry["number"], part=entry.get("volume", 0), part_title=entry.get("volume_title", ""),
                   label=label, title=title, blocks=parsed.blocks, source_id=entry["id"], source_url=entry["url"],
                   published=entry.get("date", ""), author_notes=parsed.author_notes)
