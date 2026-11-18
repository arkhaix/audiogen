"""Images for the bible: a portrait of each character and a view of each setting the story returns to.

For every subject that qualifies and has no image yet:
  1. An image pass (LLM) turns the bible's record into a description. A character's record is its bible entry
     with its facts, and the look details when a look pass (looks.py) has read its chapters. A setting's record
     is what the look pass kept of it.
  2. ComfyUI paints N candidates of it in one of the serial's styles, with the seeds seed0, seed0 + 1, ... When no
     ComfyUI answers, the job starts one and stops it at the end.
  3. The first candidate is filed with status "auto", to pick another or approve after looking.

A serial has one style or more, each with a name and a text that goes in front of every description. A subject has
one description, and an image of its own in each style.

library/<slug>/images/_briefs.yaml                      the serial's styles and each subject's description
library/<slug>/images/<style>/<kind>/<id>.yaml          a subject's record in a style: prompt, candidates, the chosen
                                                        one, earlier rounds
library/<slug>/images/<style>/<kind>/<id>/<seed>.webp   a candidate, with <seed>.thumb.webp beside it

<kind> is "characters" or "settings", and a subject's key is "<kind>/<id>". A character's second appearance, such
as a suit of armour the look pass named, is a subject of its own with the id "<character id>--<appearance>".
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path

from . import library as lib
from . import looks as look_record
from . import machine
from .comfy import IMAGE_MODELS, ComfyClient, ComfyError, image_graph
from .llm import LLMError, run_pass

IMAGE_PROMPT = lib.PROMPTS / "image_pass.system.md"
IMAGE_SCHEMA = lib.SCHEMAS / "image_pass.schema.json"
KINDS = ("characters", "settings")
DEFAULT_MODEL = "qwen21"
# width and height, multiples of 16
SIZES = {"characters": (896, 1152), "settings": (1344, 768)}
THUMBS = {"characters": 256, "settings": 448}
FRAMING = {
    "characters": "A half-length portrait of one person, turned a little to one side, against a plain dark "
                  "background. No text or lettering.",
    "settings": "A wide view of one place, seen from a little distance. No text or lettering.",
}
DEFAULT_STYLE = "Painterly book illustration with visible brushwork, muted natural colours and soft directional light."
# the name of the style a serial's first draw makes when it names none
DEFAULT_STYLE_NAME = "main"
IMAGE_FOR = ("major", "recurring")
SETTING_CHAPTERS = 2
APPEARANCE_CHAPTERS = 2
IMPORTANCE_ORDER = {"major": 0, "recurring": 1, "minor": 2, "one_off": 3}
BRIEF_BATCH = 12
DRAW_LIMIT = 24
UNDESCRIBED_SHOWN = 12
FIRST_SEED = 1000
IMAGE_TIMEOUT = 1800
COMFY_START_TIMEOUT = 300
COMFY_START_POLL = 2.0
ROUND_KEYS = ("prompt", "brief", "style", "model", "size", "basis", "seed", "status", "created_at", "candidates")


# Subjects

def split_key(key: str) -> tuple[str, str]:
    kind, _, sid = key.partition("/")
    if kind not in KINDS or not look_record.ID_RE.match(sid):
        raise ValueError(f"bad image subject {key!r}: use characters/<id> or settings/<id>")
    return kind, sid


def images_dir(slug: str) -> Path:
    return lib.serial_dir(slug) / "images"


def style_name(value) -> str:
    """A style's name, which names its folder under images/. Raises ValueError on a name that can't."""
    name = str(value or "")
    if not look_record.ID_RE.match(name) or name in KINDS:
        raise ValueError(f"bad style name {name!r}: use lower-case letters, digits and hyphens, and not "
                         f"{' or '.join(KINDS)}")
    return name


def record_path(slug: str, key: str, style: str) -> Path:
    """Where a subject's record in a style is. Raises ValueError on a key that is not a subject's and on a
    style name that names no folder."""
    kind, sid = split_key(key)
    return images_dir(slug) / style_name(style) / kind / f"{sid}.yaml"


def look_digest(look) -> str:
    """A short hash of a subject's look details, which changes when any detail does."""
    kept = [[f.get("chapter"), f.get("text"), f.get("lasting", True)] for f in look or [] if isinstance(f, dict)]
    return hashlib.sha1(json.dumps(kept, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def load_record(slug: str, key: str, style: str) -> dict | None:
    return lib.load_yaml(record_path(slug, key, style))


def records(slug: str, style: str) -> dict[str, dict]:
    """{key: record} for every subject of the serial that has an image in a style."""
    out = {}
    for kind in KINDS:
        for path in sorted((images_dir(slug) / style_name(style) / kind).glob("*.yaml")):
            rec = lib.load_yaml(path)
            if isinstance(rec, dict) and rec.get("candidates"):
                out[f"{kind}/{path.stem}"] = rec
    return out


def subjects(bible: dict, looks: dict, settings: dict) -> dict[str, dict]:
    """Every subject an image could show, as {key: {"kind", "id", "name", "look", "chapters", ...}}.

    bible: the bible, for its characters.
    looks, settings: the visual record.
    A character also has the fields of its bible entry that describe it and its "facts", and a setting its
    description. A character's "look" is its everyday one, and "appearances" has the slugs of its others. Each of
    those is a subject too, with the id "<character id>--<slug>": it has the character's fields, "of" with the
    character's id, "appearance" with the slug, the appearance's details as "look" and the everyday ones as
    "everyday". An appearance whose id can't name a file, or is a character's, is no subject, and its details
    stay with the everyday look. An id that can't name a file is left out, and so is an entry that is not a
    record. Never raises on a record a hand edit left malformed.
    """
    def listed(value, kind) -> list:
        return [item for item in value if isinstance(item, kind)] if isinstance(value, list) else []

    out, second = {}, {}
    chars = {cid: c for cid, c in (bible.get("characters") or {}).items()
             if look_record.ID_RE.match(str(cid)) and isinstance(c, dict)}
    for cid, c in chars.items():
        details = listed(looks.get(cid), dict)
        slugs = [a for a in look_record.appearances(details)
                 if look_record.ID_RE.match(f"{cid}--{a}") and f"{cid}--{a}" not in chars]

        def shown_as(f: dict) -> str:
            slug = look_record.appearance_slug(f.get("appearance"))
            return slug if slug in slugs else ""

        base = {
            "kind": "characters", "id": cid, "name": c.get("name") or cid,
            "look": [f for f in details if not shown_as(f)],
            "chapters": listed(c.get("chapters_present"), int), "facts": listed(c.get("facts"), dict),
            **{k: c.get(k) for k in ("gender", "age_group", "importance", "role", "description", "bio")},
            **({"appearances": slugs} if slugs else {})}
        out[f"characters/{cid}"] = base
        for slug in slugs:
            second[f"characters/{cid}--{slug}"] = {
                **{k: v for k, v in base.items() if k != "appearances"}, "id": f"{cid}--{slug}",
                "name": f"{base['name']}, {slug.replace('-', ' ')}", "of": cid, "appearance": slug,
                "everyday": base["look"], "look": [f for f in details if shown_as(f) == slug]}
    out.update(second)
    for sid, s in settings.items():
        if not look_record.ID_RE.match(str(sid)) or not isinstance(s, dict):
            continue
        out[f"settings/{sid}"] = {
            "kind": "settings", "id": sid, "name": s.get("name") or sid, "look": listed(s.get("look"), dict),
            "chapters": listed(s.get("chapters_present"), int), "description": s.get("description")}
    return out


def qualifies(serial: dict, subject: dict) -> bool:
    """Whether a subject gets an image without being named: a character of an importance the serial's
    image_policy covers, a second appearance of such a character that has look details from enough chapters, or
    a setting with scenes in enough chapters."""
    policy = serial.get("image_policy") or {}
    if subject["kind"] == "characters":
        if subject.get("importance") not in (policy.get("characters_for") or IMAGE_FOR):
            return False
        shown = len({f.get("chapter") for f in subject["look"]})
        return not subject.get("appearance") or shown >= int(policy.get("appearance_chapters") or APPEARANCE_CHAPTERS)
    return len(subject["chapters"]) >= int(policy.get("setting_chapters") or SETTING_CHAPTERS)


def described(subject: dict) -> bool:
    """Whether the record has something to paint a subject from: a character's bible entry, or a look detail of a
    setting."""
    return subject["kind"] == "characters" or bool(subject["look"])


def passages(look) -> list[dict]:
    """The look details a description goes by, as an image's record keeps them: each one's chapter, its text and
    the passage of the chapter that shows it. A detail of one scene is among them only when the subject has no
    other, which is when the image pass uses it."""
    details = [f for f in look or [] if isinstance(f, dict)]
    lasting = [f for f in details if f.get("lasting", True)]
    return [{k: f[k] for k in ("chapter", "text", "quote") if f.get(k) not in (None, "")}
            for f in lasting or details]


def plan(serial: dict, found: dict[str, dict], have, *, named: list[str] | None = None, redraw: bool = False,
         limit: int = DRAW_LIMIT) -> dict:
    """The subjects a draw works on.

    serial: the serial.yaml record.
    found: every subject, from subjects().
    have: the keys that have an image.
    named: the keys to draw, or None for every subject that qualifies, has no image and has a record to paint
        it from.
    redraw: draw the named subjects that have an image again.
    limit: the most subjects an unnamed draw takes. The most important characters come first, then the settings
        with the most chapters.
    Returns {"keys", "undescribed", "left", "kept"}: the subjects to draw in order, the settings that qualify and
    wait for a look detail from the text, how many more an unnamed draw left for the next one, and the named ones
    that keep the image they have. Raises ComfyError on a named subject that does not exist.
    """
    if named:
        unknown = [k for k in named if k not in found]
        if unknown:
            raise ComfyError(f"not in the bible: {', '.join(unknown)}")
        kept = [k for k in named if k in have and not redraw]
        return {"keys": [k for k in named if k not in kept], "undescribed": [], "left": 0, "kept": kept}
    wanted = [k for k, s in found.items() if k not in have and qualifies(serial, s)]
    ready = [k for k in wanted if described(found[k])]
    ready.sort(key=lambda k: (found[k]["kind"] != "characters", IMPORTANCE_ORDER.get(found[k].get("importance"), 9),
                              -len(found[k]["chapters"]), k))
    return {"keys": ready[:limit], "undescribed": [k for k in wanted if not described(found[k])],
            "left": max(0, len(ready) - limit), "kept": []}


# Briefs

def read_briefs(data) -> dict:
    """The serial's styles and its cache of descriptions, from the parsed _briefs.yaml: {"styles": {name: text},
    "briefs": {key: {"prompt", "facts", "look", "details"}}}. The styles are in the order they were made, and the
    first is the one a draw paints in when it names none. facts counts the look details a description saw, look
    is their look_digest and details is their passages(). A description written by hand has "by": "hand". A part
    of the file that is not what it should be reads as empty."""
    data = data if isinstance(data, dict) else {}
    styles = data.get("styles") if isinstance(data.get("styles"), dict) else {}
    briefs = data.get("briefs") if isinstance(data.get("briefs"), dict) else {}
    return {"styles": {str(k): v for k, v in styles.items()
                       if look_record.ID_RE.match(str(k)) and str(k) not in KINDS and isinstance(v, str) and v.strip()},
            "briefs": dict(briefs)}


def load_briefs(slug: str) -> dict:
    """read_briefs() of a serial's _briefs.yaml, or of nothing before its first draw."""
    return read_briefs(lib.load_yaml(images_dir(slug) / "_briefs.yaml", {}))


def save_briefs(slug: str, cache: dict) -> None:
    lib.replace_yaml(images_dir(slug) / "_briefs.yaml", cache)


def brief_input(serial: dict, batch: dict[str, dict], style: str | None) -> str:
    """The image pass's input for a batch of subjects.

    serial: the serial.yaml record.
    batch: {key: subject}, from subjects().
    style: the style of the serial's pictures, or None when the pass is to choose one.
    Returns the text, with one section per subject under its key.
    """
    tags = ", ".join((serial.get("source") or {}).get("tags") or [])
    lines = [f"# Images for {serial['title']} by {serial.get('author') or 'unknown'}"]
    if tags:
        lines.append(f"tags: {tags}")
    if serial.get("description"):
        lines.append("blurb: " + " ".join(str(serial["description"]).split()))
    lines.append(f"style in use: {style}" if style else "style in use: none yet, so choose one")
    for key, s in batch.items():
        lines += ["", f"## {key}", f"kind: {'character' if s['kind'] == 'characters' else 'setting'}",
                  f"name: {s['name']}"]
        if s.get("appearance"):
            lines.append(f"appearance: {s['appearance'].replace('-', ' ')}")
        elif s.get("appearances"):
            lines.append("other appearances, each with a picture of its own: "
                         + ", ".join(a.replace("-", " ") for a in s["appearances"]))
        if s["kind"] == "characters":
            lines += [f"{k}: {s.get(k)}" for k in ("gender", "age_group", "importance", "role", "description")]
            if s.get("bio"):
                lines.append(f"bio: {s['bio']}")
            # a second appearance goes by its look details, so the facts are sent with the everyday portrait alone
            if not s.get("appearance"):
                lines.append("facts:")
                lines += [f"- chapter {f.get('chapter')}: {f.get('text')}" for f in s.get("facts") or []] or ["- (none)"]
        else:
            lines.append(f"description: {s.get('description')}")
            lines.append(f"scenes in chapters: {', '.join(map(str, s['chapters'])) or 'none'}")
        for title, details in ((("look in this appearance:", s["look"]), ("everyday look:", s.get("everyday")))
                               if s.get("appearance") else (("look:", s["look"]),)):
            lines.append(title)
            lines += [f"- chapter {f.get('chapter')}{'' if f.get('lasting', True) else ' (that scene only)'}: "
                      f"{f.get('text')}" for f in details or []] or ["- (the text has given none yet)"]
    return "\n".join(lines) + "\n"


def brief_pass(serial: dict, batch: dict[str, dict], style: str | None, *, backend="auto", model="opus",
               effort=None, on_limit="api", log=print) -> tuple[str, dict[str, str]]:
    """Asks the LLM for the description an image model paints each subject from.

    serial: the serial.yaml record.
    batch: {key: subject}.
    style: the style of the serial's pictures, or None to have the pass choose one.
    backend, model, effort, on_limit: passed to run_pass.
    log: receives the progress lines.
    Returns (style, {key: description}) with every key of batch. The style is the one given, when one was.
    Raises LLMError when the pass fails or leaves a subject out.
    """
    keys, out = list(batch), {}
    for start in range(0, len(keys), BRIEF_BATCH):
        part = {k: batch[k] for k in keys[start:start + BRIEF_BATCH]}
        result = run_pass(IMAGE_PROMPT.read_text(encoding="utf-8"), brief_input(serial, part, style),
                          lib.load_json(IMAGE_SCHEMA), model=model, effort=effort, backend=backend,
                          on_limit=on_limit, log=log,
                          instruction="Write the description for every record above and answer with the JSON "
                                      "object only.")
        got = {i["id"]: " ".join(str(i["prompt"]).split()) for i in result.data["images"]}
        absent = [k for k in part if not got.get(k)]
        if absent:
            raise LLMError(f"the image pass didn't return a description for {', '.join(absent)}")
        style = style or " ".join(str(result.data.get("style") or "").split()) or DEFAULT_STYLE
        out.update({k: got[k] for k in part})
        log(f"image pass: {len(part)} descriptions via {result.backend}/{result.model}, "
            f"est ${result.cost_estimate_usd or 0:.4f}")
    return style or DEFAULT_STYLE, out


def compose(style: str, kind: str, brief: str) -> str:
    """The whole prompt of a subject: the serial's style, the framing of its kind and its description."""
    return " ".join(part.strip() for part in (style, FRAMING[kind], brief) if part and part.strip())


# ComfyUI

class ComfyServer:
    """The ComfyUI this process's prompts go to, at `url` once it is entered.

    That is the ComfyUI that answers at the address given, else one that answers on the same port of another
    address of this machine, else one this process starts and stops. So a machine never runs two. A ComfyUI this
    process starts listens on the address given, which has to be a loopback address. On Windows a job object stops
    it when this process ends, however that happens.
    """

    def __init__(self, url: str, log_path: Path, log=print):
        self.url, self.log_path, self.log = url.rstrip("/"), log_path, log
        self.proc, self.job, self.log_file = None, None, None

    def answers(self, url: str | None = None) -> bool:
        url = url or self.url
        if not machine.listening(url):
            return False
        try:
            ComfyClient(url).system_stats()
            return True
        except ComfyError:
            return False

    def elsewhere(self) -> str | None:
        """The address of a ComfyUI that answers on url's port at another address of this machine, or None.
        Only a loopback url is looked for elsewhere."""
        parts = urllib.parse.urlsplit(self.url)
        if parts.hostname not in machine.LOOPBACK:
            return None
        port = parts.port or 80
        for address in machine.local_addresses():
            other = f"{parts.scheme}://{address}:{port}"
            if machine.accepts(address, port) and self.answers(other):
                return other
        return None

    def __enter__(self) -> "ComfyServer":
        if self.answers():
            return self
        other = self.elsewhere()
        if other:
            self.log(f"   using the ComfyUI that is up at {other}")
            self.url = other
            return self
        parts = urllib.parse.urlsplit(self.url)
        if parts.hostname not in machine.LOOPBACK:
            raise ComfyError(f"ComfyUI does not answer at {self.url}, which is not on this machine to start")
        if not machine.COMFY_MAIN.is_file() or not machine.COMFY_PYTHON.exists():
            raise ComfyError(f"ComfyUI does not answer at {self.url}, and there is none to start: "
                             f"{machine.COMFY_MAIN} under {machine.COMFY_PYTHON}")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = open(self.log_path, "ab")
        args = [str(machine.COMFY_PYTHON), "-s", str(machine.COMFY_MAIN), "--disable-auto-launch",
                "--listen", parts.hostname, "--port", str(parts.port or 80)]
        self.proc = subprocess.Popen(args + (["--windows-standalone-build"] if machine.WINDOWS else []),
                                     cwd=str(machine.COMFY_MAIN.parent.parent), stdin=subprocess.DEVNULL,
                                     stdout=self.log_file, stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW if machine.WINDOWS else 0)
        if machine.WINDOWS:
            from .breeze import tie_to_this_process
            self.job = tie_to_this_process(self.proc)
        deadline = time.time() + COMFY_START_TIMEOUT
        try:
            while not self.answers():
                if self.proc.poll() is not None:
                    raise ComfyError(f"ComfyUI exited with {self.proc.returncode} while starting; see {self.log_path}")
                if time.time() > deadline:
                    raise ComfyError(f"ComfyUI did not answer within {COMFY_START_TIMEOUT} s; see {self.log_path}")
                time.sleep(COMFY_START_POLL)
        except BaseException:
            self.stop()
            raise
        self.log(f"   started ComfyUI at {self.url} for this job")
        return self

    def stop(self) -> None:
        """Stops the ComfyUI this process started. One that was already up stays up. Never raises."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.job is not None:
            from .breeze import close_handle
            close_handle(self.job)
        if self.log_file is not None:
            self.log_file.close()
        self.proc, self.job, self.log_file = None, None, None

    def __exit__(self, *exc) -> None:
        self.stop()


def comfy_up(url: str) -> str | None:
    """The address of the ComfyUI that is up on this machine: the one at url, else one that answers on url's port
    at another address of the machine, else None."""
    server = ComfyServer(url, Path("comfyui.log"))
    return server.url if server.answers() else server.elsewhere()


def _saved(client: ComfyClient, entry: dict, node: str, what: str) -> Path:
    items = (entry.get("outputs") or {}).get(node, {}).get("images") or []
    if not items:
        raise ComfyError(f"ComfyUI saved no {what}: outputs {list(entry.get('outputs') or {})}")
    path = client.output_path(items[0])
    if not path.is_file():
        raise ComfyError(f"ComfyUI's {what} is not at {path}")
    return path


def draw_candidates(client: ComfyClient, slug: str, key: str, prompt: str, *, style: str, model: str, count: int,
                    seed0: int, log=print) -> list[dict]:
    """Paints `count` candidates of one subject and puts them in the library.

    client: the ComfyUI that paints, with its output folder.
    slug, key: the serial and the subject.
    prompt: the whole prompt.
    style: the name of the style, which names the candidates' folder.
    model: a key of comfy.IMAGE_MODELS.
    count, seed0: the candidates use seeds seed0, seed0 + 1, ...
    log: receives one line per candidate.
    Returns one record per candidate, {seed, image, thumb}, with paths relative to the serial's folder. The files
    leave ComfyUI's output folder. Raises ComfyError when ComfyUI fails or a file does not appear.
    """
    kind, sid = split_key(key)
    width, height = SIZES[kind]
    folder = images_dir(slug) / style / kind / sid
    folder.mkdir(parents=True, exist_ok=True)
    # what a draw that was stopped left of this subject in ComfyUI's folder
    shutil.rmtree(machine.COMFY_OUTPUT / "images" / slug / style / kind / sid, ignore_errors=True)
    out = []
    for i in range(count):
        seed, t0 = seed0 + i, time.time()
        entry = client.run(image_graph(model=model, prompt=prompt, seed=seed, width=width, height=height,
                                       prefix=f"images/{slug}/{style}/{key}/s{seed}", thumb=THUMBS[kind]),
                           timeout=IMAGE_TIMEOUT)
        made = {"image": _saved(client, entry, "save", "image"), "thumb": _saved(client, entry, "save_thumb", "small copy")}
        cand = {"seed": seed}
        for name, src in made.items():
            dst = folder / (f"{seed}{src.suffix}" if name == "image" else f"{seed}.thumb{src.suffix}")
            shutil.copyfile(src, dst)
            src.unlink(missing_ok=True)
            cand[name] = dst.relative_to(lib.serial_dir(slug)).as_posix()
        out.append(cand)
        log(f"   {key} seed {seed}: drawn in {time.time() - t0:.0f} s")
    return out


# Records

def used_seeds(record: dict | None) -> set[int]:
    """The seeds of every candidate of a subject, in its current round and the earlier ones."""
    rounds = [record] + list(record.get("history") or []) if record else []
    return {c["seed"] for r in rounds for c in r.get("candidates") or []
            if isinstance(c, dict) and isinstance(c.get("seed"), int)}


def next_seed(record: dict | None) -> int:
    """The first seed no candidate of the subject has used, or FIRST_SEED for a subject without an image."""
    return max(used_seeds(record), default=FIRST_SEED - 1) + 1


def file_image(slug: str, key: str, *, style: str, brief: str, style_text: str, model: str, basis: dict,
               candidates: list[dict], previous: dict | None = None, note: str | None = None) -> dict:
    """Saves a subject's record in a style with new candidates and the first of them chosen.

    slug, key: the serial and the subject.
    style: the name of the style.
    brief, style_text: the description the candidates were painted from and the style's text in front of it.
    model: the key of comfy.IMAGE_MODELS that painted them.
    basis: what the description saw of the text, {"facts": the count of look details, "look": their look_digest,
        "details": their passages()}.
    candidates: one record per candidate, from draw_candidates.
    previous: the record these candidates replace, which goes under history with its candidates.
    note: why it is replaced, kept with it.
    Returns the saved record, with status "auto".
    """
    kind, sid = split_key(key)
    record = {"id": sid, "kind": kind, "status": "auto", "model": model, "style": style_text, "brief": brief,
              "prompt": compose(style_text, kind, brief), "size": list(SIZES[kind]), "basis": dict(basis),
              "seed": candidates[0]["seed"], "chosen": 0, "candidates": candidates, "created_at": lib.now_iso()}
    if previous:
        old = {k: previous.get(k) for k in ROUND_KEYS}
        old["replaced_at"] = lib.now_iso()
        if note:
            old["why_replaced"] = note
        record["history"] = list(previous.get("history") or []) + [old]
    lib.replace_yaml(record_path(slug, key, style), record)
    return record


def pick_image(slug: str, key: str, *, style: str | None = None, seed: int | None = None,
               approve: bool = True) -> dict:
    """Files another candidate of a subject as its image in a style, by seed, and marks the image approved.

    slug, key: the serial and the subject.
    style: the name of the style. None means the serial's first.
    seed: the candidate, from the current round or an earlier one. None keeps the choice.
    approve: False leaves the status as it is.
    Returns the saved record. A candidate of an earlier round brings its round back, and the current round goes
    under history. Raises FileNotFoundError for a subject without an image in the style and ValueError for an
    unknown seed or a style name that names no folder.
    """
    style = style_name(style or next(iter(load_briefs(slug)["styles"]), DEFAULT_STYLE_NAME))
    record = load_record(slug, key, style)
    if not record:
        raise FileNotFoundError(f"no image for {key} in the style {style}")
    if seed is not None:
        rounds = [record] + list(reversed(record.get("history") or []))
        found = next(((r, i) for r in rounds for i, c in enumerate(r.get("candidates") or []) if c.get("seed") == seed),
                     None)
        if found is None:
            seeds = [c.get("seed") for r in rounds for c in r.get("candidates") or []]
            raise ValueError(f"{key} has no candidate with seed {seed}; seeds: {seeds}")
        batch, chosen = found
        if batch is not record:
            old = {k: record.get(k) for k in ROUND_KEYS}
            old.update(replaced_at=lib.now_iso(), why_replaced=f"seed {seed} of an earlier round was picked")
            history = [h for h in record.get("history") or [] if h is not batch] + [old]
            record = {"id": record["id"], "kind": record["kind"], **{k: batch.get(k) for k in ROUND_KEYS},
                      "history": history}
        record.update(chosen=chosen, seed=seed)
    if approve:
        record["status"] = "approved"
    lib.replace_yaml(record_path(slug, key, style), record)
    return record


# The job

def draw(slug: str, *, characters: list[str] | None = None, settings: list[str] | None = None, candidates: int = 2,
         seed0: int | None = None, redraw: bool = False, prompt: str | None = None, redo_brief: bool = False,
         note: str | None = None, image_model: str | None = None, style: str | None = None,
         style_text: str | None = None, limit: int = DRAW_LIMIT, briefed: bool = False,
         dry_run: bool = False, comfy_url: str = "http://127.0.0.1:8188", backend="auto", model="opus", effort=None,
         on_limit="api", log=print) -> list[dict]:
    """Describes and paints the subjects that need an image in one style, and files each one's first candidate.

    slug: the serial.
    characters, settings: the ids to draw. Without either, every subject plan() picks that has no image in the
        style.
    candidates, seed0: each subject gets `candidates` images from seeds seed0, seed0 + 1, ... None starts each
        subject at the first seed it has not used, and so does a seed0 whose seeds the subject has used, because
        a candidate's file is named by its seed.
    redraw: draw the named subjects that have an image again. The image they have goes under history.
    prompt: the description for exactly one named subject, in place of the image pass's.
    redo_brief: ask the image pass again for the subjects to draw.
    note: why an image is replaced, kept with the old one.
    image_model: a key of comfy.IMAGE_MODELS. None means serial.yaml image_model, else DEFAULT_MODEL.
    style: the name of the style to paint in. None means the serial's first style, and DEFAULT_STYLE_NAME for a
        serial without one. A name the serial doesn't have makes a new style.
    style_text: the style's text from this draw on, kept for later draws. None means the text the style has.
    limit: the most subjects an unnamed draw takes.
    briefed: ask no LLM. A subject without a cached description is left for a later draw.
    dry_run: describe and paint nothing.
    comfy_url: the ComfyUI that paints.
    backend, model, effort, on_limit: passed to run_pass.
    log: receives the progress lines.
    Returns the records filed, or with dry_run the descriptions. A subject's description is asked for again when
    its look details are not the ones its cached description saw, and one description serves every style. For a
    style without a text, the image pass chooses one with the descriptions it writes and the draw keeps it. A
    draw that asks for no description gives a serial's first style DEFAULT_STYLE and raises ComfyError for a
    later style without a text. style_text is kept also when the draw finds nothing to draw. The prompts go to the ComfyUI that is
    up on this machine, and one is started only when none is. When ComfyUI fails, the subjects painted before it
    stay filed and the error is raised. Raises ComfyError, also for a style name that names no folder, and
    LLMError when the image pass fails.
    """
    if candidates < 1:
        raise ComfyError("a draw needs one candidate or more for each subject")

    # Plan
    serial = lib.load_serial(slug)
    cache = load_briefs(slug)
    try:
        name = style_name(style or next(iter(cache["styles"]), DEFAULT_STYLE_NAME))
    except ValueError as e:
        raise ComfyError(str(e)) from None
    given = " ".join((style_text or "").split())
    if given and given != cache["styles"].get(name):
        cache["styles"][name] = given
        save_briefs(slug, cache)
    found = subjects(lib.load_bible(slug), look_record.load_looks(slug), look_record.load_settings(slug))
    have = records(slug, name)
    named = [f"characters/{c}" for c in characters or []] + [f"settings/{s}" for s in settings or []]
    todo = plan(serial, found, set(have), named=named or None, redraw=redraw, limit=limit)
    keys = todo["keys"]
    if todo["kept"]:
        log(f"already drawn, kept (name them with --redraw to replace): {', '.join(todo['kept'])}")
    if todo["undescribed"]:
        shown, more = todo["undescribed"][:UNDESCRIBED_SHOWN], len(todo["undescribed"]) - UNDESCRIBED_SHOWN
        log(f"no look detail from the text yet, so not drawn unless named: {', '.join(shown)}"
            + (f" and {more} more" if more > 0 else ""))
    if prompt and len(keys) != 1:
        raise ComfyError("--prompt applies to exactly one subject; name it with --characters or --settings")
    image_model = image_model or serial.get("image_model") or DEFAULT_MODEL
    if image_model not in IMAGE_MODELS:
        raise ComfyError(f"unknown image model {image_model!r}; known: {', '.join(IMAGE_MODELS)}")
    if not keys:
        log("nothing to draw")
        return []
    log(f"drawing {len(keys)}: {', '.join(keys)}" + (f" ({todo['left']} more wait for the next draw)" if todo["left"] else ""))

    # Briefs
    kept = text = cache["styles"].get(name)
    briefs = cache["briefs"]
    seen = {k: {"facts": len(found[k]["look"]), "look": look_digest(found[k]["look"]),
                "details": passages(found[k]["look"])} for k in keys}
    if prompt:
        briefs[keys[0]] = {"prompt": " ".join(prompt.split()), **seen[keys[0]], "by": "hand"}
    stale = [k for k in keys if not (prompt and k == keys[0])
             and (redo_brief or k not in briefs or (briefs[k].get("look") != seen[k]["look"]
                                                    and briefs[k].get("by") != "hand"))]
    if briefed:
        stale = []
    if stale:
        text, got = brief_pass(serial, {k: found[k] for k in stale}, text, backend=backend, model=model,
                               effort=effort, on_limit=on_limit, log=log)
        briefs.update({k: {"prompt": words, **seen[k]} for k, words in got.items()})
    if not text and any(k in briefs for k in keys):
        if cache["styles"]:
            raise ComfyError(f"the style {name} has no text yet, and this draw asks the image pass for no "
                             f"description, so the pass can't choose one: give the style its text")
        text = DEFAULT_STYLE
    if stale or prompt or (text and text != kept):
        if text:
            cache["styles"][name] = text
        save_briefs(slug, cache)
    for k in keys:
        log(f"-- {k}\n   {briefs[k]['prompt'] if k in briefs else '(no description yet)'}")
    if dry_run:
        return [{"id": k, **briefs[k]} for k in keys if k in briefs]
    unbriefed = [k for k in keys if k not in briefs]
    if unbriefed:
        log(f"no description yet, left for the next draw: {', '.join(unbriefed)}")
        keys = [k for k in keys if k in briefs]
    if not keys:
        return []

    # Images
    busy = machine.breeze_server_pids()
    if busy:
        raise ComfyError(f"a breeze-server is running (pid {', '.join(map(str, busy))}), and the images would share "
                         f"the card with it")
    log(f"   style {name}: {text}\n   painting with {IMAGE_MODELS[image_model]['label']}, {candidates} candidate(s) "
        f"each")
    out = []
    with ComfyServer(comfy_url, machine.COMFY_OUTPUT / "images" / slug / "comfyui.log", log) as server:
        client = ComfyClient(server.url, output_dir=machine.COMFY_OUTPUT)
        try:
            for k in keys:
                previous = have.get(k)
                first = seed0 if seed0 is not None else next_seed(previous)
                if used_seeds(previous) & set(range(first, first + candidates)):
                    log(f"   {k}: a candidate has a seed from {first} on already, so the draw starts at "
                        f"{next_seed(previous)}")
                    first = next_seed(previous)
                brief = briefs[k]["prompt"]
                cands = draw_candidates(client, slug, k, compose(text, split_key(k)[0], brief), style=name,
                                        model=image_model, count=candidates, seed0=first, log=log)
                basis = {field: briefs[k].get(field) for field in ("facts", "look", "details")}
                out.append(file_image(slug, k, style=name, brief=brief, style_text=text, model=image_model,
                                      basis=basis, candidates=cands, previous=previous, note=note))
                log(f"   {k}: filed seed {first} -> images/{name}/{k}.yaml")
        finally:
            # breeze-server asks only the ComfyUI at its own address to unload, so the card is given back here
            try:
                client.free(settle=0.0)
            except ComfyError:
                pass
    return out
