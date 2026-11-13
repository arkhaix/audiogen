"""Checks a Breeze chapter's takes against the script with a speech recognizer, Qwen3-ASR in ComfyUI or Parakeet.

Breeze TTS 2 sometimes loops, repeating a word or phrase, or drops the end of a line, and the take-length guard
catches only the gross cases. This hears every take and records one entry per take in take_check.json beside the
script, with a summary under render.check in chapter.yaml.

An entry holds the take's key and audio hash, its evidence and what judge() derives from the evidence. The evidence
is in versioned blocks. "asr" has the transcript and, from Parakeet, the raw tokens with their start times,
durations and log-probabilities. "signal" has the numbers take_measure.measure reads from the audio. On a machine
that has their models, "vad" has the speech Silero VAD finds, "speaker" TitaNet small's embedding of the voice, and
"asr2" the transcript of a second recognizer, Moonshine base English, and "phone" the IPA that ZIPA small decodes
with no text to guide it. A rule in judge() fires only on a block of the current version, so a take that Qwen3-ASR
heard is judged on its words.

A take with three or more inserted or dropped words is flagged. A take with a high word error rate but fewer slips
is suspect, which is usually a name the recognizer spelled its own way. A transcript with no Latin letters is
unreadable: the recognizer picked the wrong language, so the take is unverified rather than wrong.
"""
from __future__ import annotations

import base64
import binascii
import functools
import json
import math
import re
import statistics
import time
from pathlib import Path

from . import library as lib
from . import phones
from .asr import check_ready, choose, extra_blocks, model_name, model_problems, records, words
from .parakeet_asr import ASR2_V, PHONE_V, SPEAKER_V, VAD_V
from .breeze import LEVEL_OFFSETS, TAKES_FILE, VOCAL_EVENTS, direction_class, keeps_its_pauses, voice_text
from .comfy import ComfyError

CHECK_FILE = "take_check.json"
ASR_V = 2
# the version of judge()'s rules. take_check.json records it as "rules", and verdicts under an older one are stale.
JUDGE_V = 7
# take_measure.SIGNAL_V, which this module cannot import, because take_measure needs numpy
SIGNAL_V = 1
# the blocks a machine gathers only when it has their models, with their versions
EXTRA_V = {"vad": VAD_V, "speaker": SPEAKER_V, "asr2": ASR2_V, "phone": PHONE_V}
BLOCKS = ("asr", "signal", *EXTRA_V)
KEPT = ("key", "audio", *BLOCKS)
MAX_SLIPS = 3
SUSPECT_WER = 0.34
SUBSTITUTED = 3
SUBSTITUTED_SHARE = 0.3
SHORT_WORDS = 3
NAME_HEARD = 2
NAME_DISTANCE = 0.75
JOIN_DISTANCE = 0.25
# the edits beyond the difference in length that align() first looks for
ALIGN_BAND = 2
PAUSE_OVER = 0.6
PAUSE_KEEP = 0.25
# how far inside a silent run a word's time from the recognizer may lie, whose times are on an 80 ms grid
PAUSE_SLACK = 0.2
CONTRACTIONS = {"'s": ("is", "has"), "'re": ("are",), "'ll": ("will",), "'ve": ("have",), "'d": ("would", "had"),
                "'m": ("am",), "n't": ("not",)}
# seconds of speech per word outside which a line that is judged on its length is not that line
SPEECH_PER_WORD = (0.1, 2.0)
REPORT_EVERY = 60
VERDICTS = ("ok", "suspect", "unreadable", "flagged")
LABELS = {"qwen3": "Qwen3-ASR", "parakeet": "Parakeet"}
TOKEN_FIELDS = ("tokens", "starts", "durations", "logprobs")
SIGNAL_REASONS = frozenset({"clipped", "fading", "swing", "rate", "level", "length", "drift"})
RATE_MIN_WORDS = 4
# the cast's rule for a clipped candidate, casting.CLIP_RUN
CLIP_RUN = 3
FADE_DROP = -16.0
SWING = 13.5
JUMP = 16.0
RATE_RANGE = (0.45, 2.6)
LEVEL_OFF = 12.0
BASELINE_MIN = 5
BREAKER_SHARE = 0.25
BREAKER_MIN = 4
# sound past the line's last word that a cut removes, when no recognizer heard a word in it
TAIL_OVER = 0.5
# how far the sound has to go on past the line's last word for a word heard after it to count
TAIL_WORDS = 0.5
# the words of a direction that ask for a sound after the line's words
TAIL_SOUNDS = frozenset(
    "laugh laughs laughing laughed laughter chuckle chuckles chuckling chuckled giggle giggles giggling giggled "
    "cough coughs coughing coughed sigh sighs sighing sighed sob sobs sobbing sobbed gasp gasps gasping gasped "
    "pant pants panting panted groan groans groaning groaned moan moans moaning moaned hum hums humming hummed "
    "snort snorts snorting snorted scoff scoffs scoffing scoffed cry cries crying cried wheeze wheezes wheezing "
    "wheezed sniff sniffs sniffing sniffed sniffle sniffles sniffling sniffled hiccup hiccups hiccuping hiccupping "
    "hiccuped hiccupped yawn yawns yawning yawned weep weeps weeping wept cackle cackles cackling cackled choke "
    "chokes choking choked snarl snarls snarling snarled hiss hisses hissing hissed purr purrs purring purred "
    "snicker snickers snickering snickered snigger sniggers sniggering sniggered exhale exhales exhaling exhaled "
    "inhale inhales inhaling inhaled growl growls growling growled whimper whimpers whimpering whimpered whine "
    "whines whining whined wail wails wailing wailed grunt grunts grunting grunted huff huffs huffing huffed gulp "
    "gulps gulping gulped gurgle gurgles gurgling gurgled sputter sputters sputtering sputtered splutter splutters "
    "spluttering spluttered".split())
DRIFT_SPEECH = 3.0
DRIFT_MIN = 8
DRIFT_MADS = 10.0
DRIFT_MAD_FLOOR = 0.01
# how far below the median similarity, in MADs, a take is left out of its voice's centre
DRIFT_TRIM_MADS = 4.0
# whether a take that adds a sound to a name is flagged. Off, a take's sayings and misses are still recorded.
NAME_FLAGS = True
# how far a name's window may be from a way of saying the name and still be that way, by phones.apart
NAME_APART = 0.45
# the seconds of a take on either side of a name's span that the name's window takes in
NAME_PAD = 0.3
# the seconds on either side of a name's span whose phones the name rule reads
NAME_NEAR = 0.12


def align(ref: list[str], hyp: list[str]) -> list[tuple[str, int | None, int | None]]:
    """The edits that turn ref into hyp, from a word-level Levenshtein alignment.

    ref: the words the line should say.
    hyp: the words the recognizer heard.
    Returns one (kind, i, j) per step, in order. kind is "=" for ref[i] heard as hyp[j], "sub" for ref[i] heard as
    another word hyp[j], "del" for ref[i] not heard, with j None, and "ins" for hyp[j] that the line doesn't have,
    with i None. Among the alignments with the fewest edits it gives one with the fewest substitutions, then the
    fewest deletions, then the substitutions between the most similar spellings.
    """
    if ref == hyp:
        return [("=", i, i) for i in range(len(ref))]
    n, m = len(ref), len(hyp)
    lean = m - n
    apart: dict[tuple[str, str], float] = {}
    width = abs(lean) + ALIGN_BAND

    # Costs, in the cells that an alignment of width edits or fewer can pass
    while True:
        slack = (width - abs(lean)) // 2
        low, high = min(0, lean) - slack, max(0, lean) + slack
        rows = [(0, [((j, 0, 0, j, 0.0), "ins") for j in range(min(m, high) + 1)])]
        for i in range(1, n + 1):
            first = max(0, i + low)
            above_first, above = rows[i - 1]
            above_last = above_first + len(above) - 1
            word, row = ref[i - 1], []
            for j in range(first, min(m, i + high) + 1):
                if j == 0:
                    row.append(((i, 0, i, 0, 0.0), "del"))
                    continue
                s = above[j - 1 - above_first][0]
                if word == hyp[j - 1]:
                    row.append((s, "="))
                    continue
                pair = (word, hyp[j - 1])
                if pair not in apart:
                    apart[pair] = spelling_distance(*pair)
                found = [((s[0] + 1, s[1] + 1, s[2], s[3], s[4] + apart[pair]), "sub")]
                if j <= above_last:
                    d = above[j - above_first][0]
                    found.append(((d[0] + 1, d[1], d[2] + 1, d[3], d[4]), "del"))
                if j > first:
                    k = row[-1][0]
                    found.append(((k[0] + 1, k[1], k[2], k[3] + 1, k[4]), "ins"))
                row.append(min(found))
            rows.append((first, row))
        # with more edits than width, a better alignment may leave the cells
        if rows[n][1][-1][0][0] <= width:
            break
        width = 2 * width + 2

    # Steps, from the last cell back
    out, i, j = [], n, m
    while i or j:
        kind = rows[i][1][j - rows[i][0]][1]
        if kind in ("=", "sub"):
            i, j = i - 1, j - 1
            out.append((kind, i, j))
        elif kind == "del":
            i -= 1
            out.append((kind, i, None))
        else:
            j -= 1
            out.append((kind, None, j))
    return out[::-1]


def spelling_distance(a: str, b: str) -> float:
    """The Levenshtein distance between two spellings as a share of the longer one, from 0.0 for equal to 1.0."""
    if not a or not b:
        return 0.0 if a == b else 1.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        row = [i]
        for j, cb in enumerate(b, 1):
            row.append(min(prev[j - 1] + (ca != cb), prev[j] + 1, row[j - 1] + 1))
        prev = row
    return prev[-1] / max(len(a), len(b))


def join_compounds(edits: list[tuple], ref: list[str], hyp: list[str]) -> list[tuple]:
    """edits with a word that was heard as two, or two that were heard as one, counted as heard.

    edits: from align(ref, hyp).
    Returns the edits with each such pair of steps turned into two "=" steps, so "self aware" matches "selfaware",
    "shadowchain" matches "shadow chain" and "why's" matches "why is". The two words run together may differ from
    the one word by JOIN_DISTANCE of their spelling, so "marry anna" matches "marianna".
    """
    def same(one: str, first: str, second: str) -> bool:
        if spelling_distance(first + second, one) <= JOIN_DISTANCE:
            return True
        return any(one == first + ending and second in said for ending, said in CONTRACTIONS.items())

    out = list(edits)
    for k in range(len(out) - 1):
        a, b = out[k], out[k + 1]
        kinds = {a[0], b[0]}
        if kinds == {"sub", "del"}:
            i, j = sorted(x[1] for x in (a, b)), next(x[2] for x in (a, b) if x[0] == "sub")
            if i[1] == i[0] + 1 and same(hyp[j], ref[i[0]], ref[i[1]]):
                out[k], out[k + 1] = ("=", i[0], j), ("=", i[1], j)
        elif kinds == {"sub", "ins"}:
            i, j = next(x[1] for x in (a, b) if x[0] == "sub"), sorted(x[2] for x in (a, b))
            if j[1] == j[0] + 1 and same(ref[i], hyp[j[0]], hyp[j[1]]):
                out[k], out[k + 1] = ("=", i, j[0]), ("=", i, j[1])
    return out


def absorb_names(edits: list[tuple], tokens: list[dict], hyp: list[str]) -> tuple[list[tuple], dict]:
    """Lets each name in a line stand for the way the recognizer spelled it.

    edits: from align and join_compounds, for the line's words against hyp.
    tokens: the line's token map. Neighbouring words of one term are one name.
    hyp: the words the recognizer heard.
    Returns (edits, names). A name is compared with the heard words in its place, and with an inserted word beside
    them when that is no further from its spelling. They match when they are within NAME_DISTANCE of the name's
    spelling and are at most NAME_HEARD words, or one word more than the name has. The name's steps then become
    "name" steps, which count as heard. A name that matches nothing keeps its steps, so a dropped name is a dropped
    word. names maps each term to what was heard for it in this take, in order, for the names that were heard as
    written or matched.
    """
    out, names, taken = list(edits), {}, set()
    at: dict[int, list[int]] = {}
    for k, e in enumerate(out):
        if e[1] is not None:
            at.setdefault(e[1], []).append(k)
    i = 0
    while i < len(tokens):
        term = tokens[i]["name"]
        end = i + 1
        while term and end < len(tokens) and tokens[end]["name"] == term:
            end += 1
        if term:
            first, last = min(at[i]), max(at[end - 1])
            inside = [k for k in range(first, last + 1) if out[k][2] is not None]
            if all(out[k][0] == "=" for k in range(first, last + 1)):
                names.setdefault(term, []).append(" ".join(hyp[out[k][2]] for k in inside))
            else:
                before = [first - 1] if first and out[first - 1][0] == "ins" and first - 1 not in taken else []
                after = [last + 1] if last + 1 < len(out) and out[last + 1][0] == "ins" else []
                limit = max(NAME_HEARD, end - i + 1)
                wanted = "".join(tokens[n]["w"] for n in range(i, end))
                best = None
                for steps in (inside, inside + after, before + inside, before + inside + after):
                    if steps and len(steps) <= limit:
                        gap = spelling_distance(wanted, "".join(hyp[out[k][2]] for k in steps))
                        if best is None or gap <= best[0]:
                            best = (gap, steps)
                if best and best[0] <= NAME_DISTANCE:
                    names.setdefault(term, []).append(" ".join(hyp[out[k][2]] for k in best[1]))
                    for k in set(range(first, last + 1)) | set(best[1]):
                        out[k] = ("name", out[k][1], out[k][2])
                        taken.add(k)
        i = end
    return out, names


def current(block, version: int) -> bool:
    """Whether an evidence block is a dict of the version the rules read."""
    return isinstance(block, dict) and block.get("v") == version


def embedding(block) -> list[float] | None:
    """The embedding a speaker block holds, at length 1, or None when the block isn't of SPEAKER_V or doesn't hold
    one."""
    scale = block.get("scale") if current(block, SPEAKER_V) else None
    if (not finite(scale) or not 0 < scale <= 1
            or not isinstance(block.get("q"), str)):
        return None
    try:
        raw = base64.b64decode(block["q"], validate=True)
    except (binascii.Error, ValueError):
        return None
    values = [(b - 256 if b > 127 else b) * scale for b in raw]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values] if norm > 0 else None


def packed(values: list[float]) -> dict:
    """values at length 1 as a speaker block holds an embedding: {"v", "scale", "q"}, which embedding() reads back."""
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    top = max((abs(v) / norm for v in values), default=0.0) or 1.0
    q = bytes(max(-127, min(127, round(v / norm / top * 127))) & 0xFF for v in values)
    return {"v": SPEAKER_V, "scale": round(top / 127, 8), "q": base64.b64encode(q).decode("ascii")}


def similarity(a: list[float], b: list[float]) -> float:
    """The cosine similarity of two embeddings.

    a, b: the embeddings, each at length 1 and of the same length.
    Returns their dot product, from -1.0 to 1.0.
    """
    return sum(x * y for x, y in zip(a, b))


def word_edits(heard_text: str, tokens: list[dict]) -> tuple[list[tuple], dict, list[str]]:
    """The line's words against a transcript: (edits, names, heard words), from align, join_compounds and
    absorb_names."""
    expected = [t["w"] for t in tokens]
    heard = words(heard_text)
    edits, names = absorb_names(join_compounds(align(expected, heard), expected, heard), tokens, heard)
    return edits, names, heard


def trailing_words(edits: list[tuple]) -> int:
    """How many words a recognizer heard after the line's last word.

    edits: the line's words against the heard words, from word_edits.
    Returns the count, and 0 when the recognizer didn't hear the line's last word.
    """
    count = 0
    for kind, _, _ in reversed(edits):
        if kind == "ins":
            count += 1
        else:
            return 0 if kind == "del" else count
    return 0


def ends_heard(edits: list[tuple]) -> bool:
    """Whether a recognizer heard the line's last word, by the edits from word_edits."""
    return next((kind != "del" for kind, i, _ in reversed(edits) if i is not None), False)


def finite(value) -> bool:
    """Whether value is an int or a float that is not a bool, an infinity or NaN."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def tail_after(asr: dict, signal: dict, vad, edits: list[tuple]) -> tuple[float | None, list[float] | None]:
    """The sound after the line's last word in a take.

    asr, signal: the take's blocks, both of the current versions.
    vad: the take's vad block, or None.
    edits: the line's words against Parakeet's words, from word_edits.
    Returns (seconds, cut). seconds is how far the take's sound goes on past the end of the last word of the line
    that Parakeet heard, by the speech frames of the signal block or the speech in a vad block of VAD_V, whichever
    ends later. It is None when the token times can't place that word or neither block has an end. cut is [start,
    end] in seconds of the take's file, from inside the first silent run after that word to the end of the file,
    when seconds is over TAIL_OVER and such a run lies before the sound ends. The run has to start after the word
    starts, and at most PAUSE_SLACK before it ends. Else cut is None.
    """
    timed = word_times(asr)
    heard_at = [j for kind, i, j in edits if kind in ("=", "name", "sub") and i is not None and j is not None]
    if not timed or not heard_at or max(heard_at) >= len(timed):
        return None, None
    first, last = timed[max(heard_at)][1:]
    ends = []
    speech = signal.get("speech")
    if isinstance(speech, list) and len(speech) == 2 and finite(speech[1]):
        ends.append(speech[1])
    segments = vad.get("segments") if current(vad, VAD_V) else None
    if isinstance(segments, list):
        ends += [seg[1] for seg in segments if isinstance(seg, list) and len(seg) == 2 and finite(seg[1])]
    if not ends or not finite(last) or not finite(first):
        return None, None
    end = max(ends)
    seconds = round(end - last, 2)
    length, runs = signal.get("seconds"), signal.get("silences")
    if seconds <= TAIL_OVER or not finite(length) or not isinstance(runs, list):
        return seconds, None
    for run in runs:
        if (isinstance(run, list) and len(run) == 2 and all(finite(n) for n in run)
                and max(first, last - PAUSE_SLACK) <= run[0] < run[1] < end and run[0] + PAUSE_KEEP / 2 < length):
            return seconds, [round(run[0] + PAUSE_KEEP / 2, 2), round(length, 2)]
    return seconds, None


def word_times(asr: dict) -> list[tuple[str, float, float]] | None:
    """When each heard word starts and ends, from the recognizer's tokens.

    asr: a take's asr block.
    Returns [(word, start, end), ...] in seconds, with the words that asr.words gives for the transcript, or None
    when the block doesn't have token times or its tokens don't give those words. A punctuation token doesn't
    move a word's end.
    """
    tokens, starts, durations = (asr.get(f) for f in ("tokens", "starts", "durations"))
    if not all(isinstance(f, list) for f in (tokens, starts, durations)) or not len(tokens) == len(starts) == len(durations):
        return None
    spans: list[list] = []
    for token, start, length in zip(tokens, starts, durations):
        if not isinstance(token, str) or not all(isinstance(n, (int, float)) for n in (start, length)):
            return None
        spoken = any(c.isalnum() for c in token)
        if token.startswith(" ") or not spans:
            spans.append([token.strip(), start if spoken else None, start + length if spoken else None])
        else:
            spans[-1][0] += token
            if spoken:
                spans[-1][1] = start if spans[-1][1] is None else spans[-1][1]
                spans[-1][2] = start + length
    out = [(w, a, b) for text, a, b in spans if a is not None for w in span_words(text)]
    return out if [w for w, _, _ in out] == words(asr.get("heard") or "") else None


@functools.lru_cache(maxsize=1 << 15)
def span_words(text: str) -> tuple[str, ...]:
    """The words asr.words gives for the text of one span of a transcript."""
    return tuple(words(text))


def name_spans(asr: dict, phone, tokens: list[dict], edits: list[tuple]) -> list[dict]:
    """Each name of a line as a take said it.

    asr: the take's asr block, of ASR_V.
    phone: the take's phone block, or None.
    tokens: the line's token map.
    edits: the line's words against the heard words, from word_edits.
    Returns {"term", "inside", "window", "near", "said", "heard", "before", "after"} for each name of the line, in
    order, whose place the recognizer's times give. inside, window and near are phones: those that start inside
    the span of the words heard in the name's place, those from NAME_PAD before that span to NAME_PAD after it,
    and those from NAME_NEAR before it to NAME_NEAR after it. said is the name's words as the voice was sent
    them, heard the words heard in their place, and before and after the line's words on either side of the name,
    "" at an end of the line. A name that no heard word stands for is left out. [] without a phone block of
    PHONE_V or without token times.
    """
    symbols, starts = (phone.get(f) for f in ("phones", "starts")) if current(phone, PHONE_V) else (None, None)
    timed = word_times(asr)
    if (not timed or not isinstance(symbols, str) or not isinstance(starts, list) or len(symbols) != len(starts)
            or not all(finite(s) for s in starts)):
        return []
    at: dict[int, list[int]] = {}
    for step, e in enumerate(edits):
        if e[1] is not None:
            at.setdefault(e[1], []).append(step)
    out, i = [], 0
    while i < len(tokens):
        term = tokens[i]["name"]
        end = i + 1
        while term and end < len(tokens) and tokens[end]["name"] == term:
            end += 1
        if term and i in at and end - 1 in at:
            first, last = min(at[i]), max(at[end - 1])
            while first and edits[first - 1][0] == "name" and edits[first - 1][1] is None:
                first -= 1
            while last + 1 < len(edits) and edits[last + 1][0] == "name" and edits[last + 1][1] is None:
                last += 1
            heard_at = sorted({edits[s][2] for s in range(first, last + 1) if edits[s][2] is not None})
            if heard_at and heard_at[-1] < len(timed):
                t0, t1 = timed[heard_at[0]][1], timed[heard_at[-1]][2]
                inside, window, near = ("".join(p for p, s in zip(symbols, starts) if t0 - pad <= s < t1 + pad)
                                        for pad in (0.0, NAME_PAD, NAME_NEAR))
                if window:
                    out.append({"term": term, "inside": inside, "window": window, "near": near,
                                "said": " ".join(t["w"] for t in tokens[i:end]),
                                "heard": " ".join(timed[j][0] for j in heard_at),
                                "before": tokens[i - 1]["w"] if i else "",
                                "after": tokens[end]["w"] if end < len(tokens) else ""})
        i = end
    return out


def picked_spellings(bible: dict) -> dict[str, str]:
    """{term: spelling} for the names the take check holds to a spelling: those that have a say-as.

    bible: the serial's bible.
    """
    return {str(term): str(entry["say"]) for term, entry in (bible.get("pronunciations") or {}).items()
            if isinstance(entry, dict) and entry.get("say")}


def stale(data, bible: dict | None = None) -> bool:
    """Whether the verdicts of a take_check.json need judging again from their evidence.

    data: the file's content.
    bible: the serial's bible, or None to compare the rules alone.
    Returns True when the file's rules are not JUDGE_V, or when the picked spellings it was judged with, under
    "names", are not the ones the bible has now.
    """
    if not isinstance(data, dict) or data.get("rules") != JUDGE_V:
        return True
    held = data.get("names") if isinstance(data.get("names"), dict) else {}
    return bible is not None and held != picked_spellings(bible)


def pause_cuts(asr: dict, signal: dict, edits: list[tuple], tokens: list[dict]) -> list[list[float]]:
    """The cuts that shorten each long pause that no punctuation explains.

    asr, signal: the take's blocks, both of the current version.
    edits: the alignment of the line's words with the heard words, after join_compounds and absorb_names.
    tokens: the line's token map.
    Returns [[start, end], ...] in seconds of the take's file, each inside one silent run of PAUSE_OVER or more
    that lies between two heard words. Both words are words of the line, heard right, and neighbours in it with
    nothing but a space between them. A cut leaves PAUSE_KEEP of the run. A run that the token times can't place
    between two words gets no cut.
    """
    timed, runs = word_times(asr), signal.get("silences")
    if timed is None or not isinstance(runs, list):
        return []
    first, last = {}, {}
    for kind, i, j in edits:
        if i is not None and j is not None and kind in ("=", "name"):
            first.setdefault(j, i)
            last[j] = i
    cuts = []
    for run in runs:
        if not isinstance(run, list) or len(run) != 2 or not all(isinstance(n, (int, float)) for n in run):
            continue
        a, b = run
        before = [j for j, (_, _, end) in enumerate(timed) if end <= a + PAUSE_SLACK]
        after = [j for j, (_, start, _) in enumerate(timed) if start >= b - PAUSE_SLACK]
        if b - a < PAUSE_OVER or not before or not after or after[0] != before[-1] + 1:
            continue
        i0, i1 = last.get(before[-1]), first.get(after[0])
        if i0 is None or i1 != i0 + 1 or tokens[i0]["after"]:
            continue
        cuts.append([round(a + PAUSE_KEEP / 2, 2), round(b - PAUSE_KEEP / 2, 2)])
    return cuts


def signal_reasons(signal: dict, words_said: int, baseline: dict | None, offset: float) -> list[str]:
    """The reasons the signal of a take gives for a retake.

    signal: the take's signal block, of SIGNAL_V.
    words_said: how many words the line has.
    baseline: the voice's baseline for the chapter, or None.
    offset: how far from the voice's ordinary level the line's direction puts it, in dB.
    Returns the reasons in a fixed order. "clipped" is a run of CLIP_RUN full-scale samples or more. "fading" is an
    ending FADE_DROP or further below the speech before it. "swing" is a spread of SWING or a step of JUMP in
    the loudness of the take's one-second windows. "rate" and "level" compare the take with the baseline, and they
    need BASELINE_MIN takes of the voice: "rate" for a line of RATE_MIN_WORDS words or more whose seconds of speech
    per word are outside RATE_RANGE times the voice's median, and "level" for a take over LEVEL_OFF from the
    voice's median level plus offset. A measure the block doesn't have fires nothing.
    """
    def number(name: str):
        value = signal.get(name)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    reasons = []
    if (number("clip_run") or 0) >= CLIP_RUN:
        reasons.append("clipped")
    if number("tail_drop") is not None and number("tail_drop") <= FADE_DROP:
        reasons.append("fading")
    if (number("swing") or 0) >= SWING or (number("jump") or 0) >= JUMP:
        reasons.append("swing")
    known = isinstance(baseline, dict) and isinstance(baseline.get("n"), int) and baseline["n"] >= BASELINE_MIN
    rate = baseline.get("rate") if known else None
    if (words_said >= RATE_MIN_WORDS and number("active") is not None and isinstance(rate, list) and rate
            and isinstance(rate[0], (int, float)) and rate[0] > 0):
        ratio = number("active") / words_said / rate[0]
        if not RATE_RANGE[0] <= ratio <= RATE_RANGE[1]:
            reasons.append("rate")
    middle = baseline.get("level") if known else None
    if (number("level") is not None and isinstance(middle, list) and middle and isinstance(middle[0], (int, float))
            and abs(number("level") - middle[0] - offset) > LEVEL_OFF):
        reasons.append("level")
    return reasons


def judge(asr: dict | None, signal: dict | None, tokens: list[dict], baseline: dict | None = None, *,
          cut_pauses: bool = True, offset: float = 0.0, vad: dict | None = None, speaker: dict | None = None,
          asr2: dict | None = None, sounds: bool = False, plain: bool = False, phone: dict | None = None,
          said: dict | None = None) -> dict:
    """The verdict for one take, from its evidence.

    asr: the take's asr block, or None. It is read only when it is of ASR_V.
    signal: the take's signal block, or None. It is read only when it is of SIGNAL_V.
    tokens: the line's token map, from breeze.voice_text.
    baseline: the voice's baseline for the chapter, from baselines(), or None.
    cut_pauses: whether the take's pauses may be shortened. False for a line that keeps its pauses.
    offset: how far from the voice's ordinary level the line's direction puts it, in dB.
    vad, speaker, asr2: the take's other blocks, or None. Each is read only when it is of its version in EXTRA_V.
    sounds: whether the line asks for a sound after its words, from asks_for_sound.
    plain: whether a take of the line can show its voice's timbre, from shows_timbre.
    phone: the take's phone block, or None. It is read only when it is of PHONE_V.
    said: {term: spelling} for the names that are held to a spelling, from picked_spellings, or None.
    Returns {"words", "sub", "del", "ins", "wer", "names", "verdict", "reasons", "repairs"} with a verdict from
    VERDICTS. reasons names each rule that calls for a retake, and the verdict is flagged when it holds any.
    repairs holds the fixes the join applies, as {"cuts": [[start, end], ...]} in seconds of the take's file. The
    result also has "errors2" and "wer2", the second recognizer's word errors, when asr2 is current, "tail", the
    seconds of sound after the line's last word, when the blocks place it, and "similarity", the take's voice
    against its voice's centre, when the speaker block and the baseline have both. With a current phone block it
    has "said", {term: [[inside, window], ...]} from name_spans, and "missed", the terms of said that this take
    says with an added sound, by phones.adds. A missed term is also the reason "name" while NAME_FLAGS is on. A take
    without an asr block of ASR_V is unreadable, with no word counted wrong, because no rule fires without its
    evidence. Uses the standard library only, and never raises on a block that lacks a field.
    """
    expected = [t["w"] for t in tokens]
    if not current(asr, ASR_V) or not isinstance(asr.get("heard"), str):
        return {"words": len(expected), "sub": 0, "del": 0, "ins": 0, "wer": 0.0, "names": {},
                "verdict": "unreadable", "reasons": [], "repairs": {"cuts": []}}
    heard_text = asr["heard"]

    # Words
    edits, names, heard = word_edits(heard_text, tokens)
    sub, dele, ins = (sum(e[0] == kind for e in edits) for kind in ("sub", "del", "ins"))
    wer = round((sub + dele + ins) / max(1, len(expected)), 3)
    short = 1 <= len(expected) <= SHORT_WORDS
    by_length = (short or sum(1 for t in tokens if not t["name"]) < 2) and bool(expected)
    reasons = []
    if not by_length or heard:
        if ins >= MAX_SLIPS:
            reasons.append("inserted")
        if dele >= MAX_SLIPS:
            reasons.append("dropped")
        if sub >= SUBSTITUTED and sub >= SUBSTITUTED_SHARE * len(expected):
            reasons.append("substituted")
        if not short and any(e[0] == "del" and e[1] == len(expected) - 1 for e in edits):
            reasons.append("ending")
    extra: dict = {}
    edits2 = None
    if current(asr2, ASR2_V) and isinstance(asr2.get("heard"), str):
        edits2 = word_edits(asr2["heard"], tokens)[0]
        extra["errors2"] = sum(e[0] in ("sub", "del", "ins") for e in edits2)
        extra["wer2"] = round(extra["errors2"] / max(1, len(expected)), 3)

    # A line too short for its words to prove much
    speech = signal.get("active") if current(signal, SIGNAL_V) else None
    per_word = speech / len(expected) if isinstance(speech, (int, float)) and expected else None
    odd_length = per_word is not None and not SPEECH_PER_WORD[0] <= per_word <= SPEECH_PER_WORD[1]
    if by_length and short and heard and dele + ins:
        reasons.append("short")
    elif by_length and odd_length:
        reasons.append("length")

    # Signal
    trim = None
    if current(signal, SIGNAL_V):
        reasons += signal_reasons(signal, len(expected), baseline, offset)
        tail, trim = tail_after(asr, signal, vad, edits)
        if tail is not None:
            extra["tail"] = tail
            if (tail > TAIL_WORDS and not short and not sounds
                    and (trailing_words(edits) or (edits2 and trailing_words(edits2)))):
                reasons.append("tail")

    # Voice
    voice = embedding(speaker)
    centre = embedding(baseline.get("centre")) if isinstance(baseline, dict) else None
    if voice and centre and len(voice) == len(centre):
        extra["similarity"] = round(similarity(voice, centre), 3)
        middle = baseline.get("similarity")
        if (plain and isinstance(middle, list) and len(middle) == 2 and all(isinstance(n, (int, float)) for n in middle)
                and isinstance(baseline.get("m"), int) and baseline["m"] >= DRIFT_MIN
                and extra["similarity"] < middle[0] - DRIFT_MADS * max(middle[1], DRIFT_MAD_FLOOR)):
            reasons.append("drift")

    # Names
    if current(phone, PHONE_V):
        spans = name_spans(asr, phone, tokens, edits)
        extra["said"] = {}
        for span in spans:
            extra["said"].setdefault(span["term"], []).append([span["inside"], span["window"]])
        held = said if isinstance(said, dict) else {}
        extra["missed"] = sorted({s["term"] for s in spans if s["term"] in held
                                  and phones.adds(s["said"], s["heard"], s["near"], s["after"], s["before"])})
        if extra["missed"] and NAME_FLAGS:
            reasons.append("name")

    # Verdict
    suspect = wer > SUSPECT_WER and (len(expected) >= 2 or not heard)
    if not reasons and suspect and not short and not by_length and extra.get("wer2", 0.0) > SUSPECT_WER:
        reasons.append("garbled")
    if re.search(r"[^\W\d_]", heard_text) and not re.search(r"[A-Za-z]", heard_text):
        verdict = "unreadable"
    elif reasons:
        verdict = "flagged"
    elif by_length and not heard and per_word is not None:
        verdict = "ok"
    elif suspect:
        verdict = "suspect"
    else:
        verdict = "ok"
    cuts = pause_cuts(asr, signal, edits, tokens) if cut_pauses and current(signal, SIGNAL_V) else []
    if (trim and cut_pauses and not sounds and ends_heard(edits) and not trailing_words(edits)
            and (edits2 is None or (ends_heard(edits2) and not trailing_words(edits2)))):
        cuts.append(trim)
    return {"words": len(expected), "sub": sub, "del": dele, "ins": ins, "wer": wer, "names": names,
            "verdict": verdict, "reasons": [] if verdict == "unreadable" else reasons, "repairs": {"cuts": cuts},
            **extra}


def classify(expected: list[str], heard_text: str) -> dict:
    """The verdict for one take from its words alone.

    expected: the script line's words as the voice says them, from expected_words.
    heard_text: the recognizer's transcript of the take.
    Returns what judge returns, with the transcript as "heard".
    """
    tokens = [{"w": w, "name": "", "after": ""} for w in expected]
    return {"heard": heard_text, **judge({"v": ASR_V, "heard": heard_text}, None, tokens)}


def expected_words(line: dict, bible: dict) -> list[str]:
    """The words a take of this script line should say, with the bible's say-as overrides applied."""
    return [t["w"] for t in voice_text(line, bible)[1]]


def evidence(entry: dict) -> tuple[dict | None, dict | None]:
    """The asr and signal blocks of a saved entry.

    entry: a take_check.json entry of any age.
    Returns (asr, signal), each None when the entry lacks it. An entry from before the blocks gives its transcript
    as a version 1 asr block.
    """
    asr = entry.get("asr")
    if not isinstance(asr, dict):
        asr = ({"v": 1, "engine": asr or "qwen3", "heard": entry["heard"]}
               if isinstance(entry.get("heard"), str) else None)
    signal = entry.get("signal")
    return asr, signal if isinstance(signal, dict) else None


def heard(entry: dict) -> str | None:
    """The transcript a saved entry holds, or None."""
    asr = evidence(entry)[0]
    return asr.get("heard") if asr and isinstance(asr.get("heard"), str) else None


def gathered(engine: str, extras=frozenset()) -> tuple[str, ...]:
    """The blocks a hearing with this recognizer gathers: asr, then for Parakeet signal and each of extras."""
    return ("asr", "signal", *(b for b in EXTRA_V if b in extras)) if engine == "parakeet" else ("asr",)


def missing(old: dict, take: dict, engine: str, extras=frozenset()) -> tuple[str, ...]:
    """The blocks a hearing of this take has to gather, given a saved entry.

    old: a take_check.json entry, or {}.
    take: the take's breeze_takes.json entry.
    engine: "qwen3" or "parakeet".
    extras: the blocks of EXTRA_V this machine gathers, from asr.extra_blocks.
    Returns () when the entry holds this recognizer's evidence for this take's audio in the current versions, and
    else the blocks to gather, in the order of gathered(). An entry from Parakeet needs its tokens, a signal block
    of SIGNAL_V and a current block for each of extras. When only blocks of extras are missing or out of date,
    those are all it returns. An entry from Qwen3-ASR needs only its transcript.
    """
    asr = old.get("asr")
    every = gathered(engine, extras)
    if (old.get("key") != take.get("key") or old.get("audio") != take.get("audio") or not current(asr, ASR_V)
            or asr.get("engine") != engine or not isinstance(asr.get("heard"), str)):
        return every
    if engine != "parakeet":
        return ()
    if not isinstance(asr.get("tokens"), list) or not current(old.get("signal"), SIGNAL_V):
        return every
    return tuple(b for b in every[2:] if not current(old.get(b), EXTRA_V[b]))


def reusable(old: dict, take: dict, engine: str, extras=frozenset()) -> bool:
    """Whether a saved entry holds this recognizer's evidence for this take's audio, in the current versions.

    old, take, engine, extras: as missing() takes them.
    """
    return not missing(old, take, engine, extras)


def badness(result: dict) -> tuple:
    """The rank of a judged take: (verdict, word errors, name score, signal reasons, prosody rank).

    result: a take_check.json entry.
    Each slot is smaller for a better take, and None when the entry doesn't have evidence for it. An earlier slot
    outranks a later one. word errors counts the second recognizer's beside Parakeet's when the entry has them.
    name score counts the names the take says with an added sound, while NAME_FLAGS is on. Compare two ranks
    with better(), which skips the slots either lacks.
    """
    reasons = result.get("reasons") if isinstance(result.get("reasons"), list) else []
    signal = sum(r in SIGNAL_REASONS for r in reasons) if current(result.get("signal"), SIGNAL_V) else None
    second = result.get("errors2") if isinstance(result.get("errors2"), int) else 0
    names = len(result["missed"]) if NAME_FLAGS and isinstance(result.get("missed"), list) else None
    return VERDICTS.index(result["verdict"]), result["sub"] + result["del"] + result["ins"] + second, names, signal, None


def better(new: dict, old: dict) -> bool:
    """Whether the take judged as new ranks above the take judged as old, on the slots both have.

    The word errors count both recognizers' only when both takes have both, and Parakeet's alone otherwise.
    """
    ranks = [list(badness(new)), list(badness(old))]
    if isinstance(new.get("errors2"), int) != isinstance(old.get("errors2"), int):
        for rank, result in zip(ranks, (new, old)):
            rank[1] = result["sub"] + result["del"] + result["ins"]
    for a, b in zip(*ranks):
        if a is not None and b is not None and a != b:
            return a < b
    return False


def hear(todo: list[tuple[str, dict]], engine: str, comfy_url: str, log, keep: dict | None = None) -> dict:
    """Gathers the evidence for takes.

    todo: (line, take) per take, where take is its breeze_takes.json entry.
    engine: "qwen3" or "parakeet".
    comfy_url: the ComfyUI that runs Qwen3-ASR.
    log: receives a progress line every REPORT_EVERY seconds.
    keep: {line: saved entry for the take's audio}. For a line whose entry lacks only blocks of EXTRA_V, only those
        are gathered and the entry keeps its other blocks. Every other line gets every block this machine gathers,
        and a block of EXTRA_V that this machine doesn't gather stays from the saved entry when it is current.
    Returns {line: entry} where an entry has the take's key, its audio hash when the take has one, the asr block
    and, from Parakeet, the signal block and the blocks of EXTRA_V this machine has the models for. Raises
    ComfyError when the recognizer cannot run here or fails.
    """
    found = {}
    if not todo:
        return found
    check_ready(engine, comfy_url)
    extras = extra_blocks() if engine == "parakeet" else set()
    for problem in (model_problems().values() if engine == "parakeet" else ()):
        log(f"   {problem}, so it is left out")
    every = gathered(engine, extras)
    groups: dict[tuple[str, ...], list[tuple[str, dict]]] = {}
    for k, take in todo:
        blocks = (missing(keep[k], take, engine, extras) or every) if keep and k in keep else every
        groups.setdefault(blocks, []).append((k, take))
    start = last_report = time.time()
    done = 0
    for blocks, group in groups.items():
        files = [Path(take["file"]) for _, take in group]
        more = {} if engine != "parakeet" or blocks == ("asr", "signal") else {"blocks": blocks}
        for i, record in records(files, engine=engine, comfy_url=comfy_url, **more):
            k, take = group[i]
            entry = {"key": take["key"], **({"audio": take["audio"]} if take.get("audio") else {})}
            if "asr" in blocks:
                asr = {"v": ASR_V, "engine": engine, "heard": str(record.get("text") or "")}
                asr.update({f: record[f] for f in TOKEN_FIELDS if isinstance(record.get(f), list)})
                entry["asr"] = asr
            else:
                entry.update({b: keep[k][b] for b in BLOCKS if b in keep[k]})
            entry.update({b: record[b] for b in BLOCKS[1:] if b in blocks and isinstance(record.get(b), dict)})
            for b, version in EXTRA_V.items():
                if b not in entry and keep and k in keep and current(keep[k].get(b), version):
                    entry[b] = keep[k][b]
            found[k] = entry
            done += 1
            now = time.time()
            if now - last_report >= REPORT_EVERY:
                last_report = now
                per = (now - start) / done
                log(f"   [{(now - start) / 60:.0f} min] checked {done}/{len(todo)}, {per:.1f} s/take, "
                    f"~{per * (len(todo) - done) / 60:.0f} min left")
    return found


def spread(values: list[float]) -> list[float]:
    """[median, median absolute deviation] of values, rounded to three places."""
    middle = statistics.median(values)
    return [round(middle, 3), round(statistics.median(abs(v - middle) for v in values), 3)]


def baselines(entries: dict, takes: dict, lines: list[dict], bible: dict) -> dict:
    """Each voice's baseline for the chapter, from the takes whose signal block is current.

    entries: {line: entry} with the evidence of the chapter's takes.
    takes: {line: breeze_takes.json entry}, which names each take's voice.
    lines: the script lines.
    bible: the serial's bible.
    Returns {voice: {"n", "level": [median, spread], "rate": [median, spread]}}. level is in LKFS. rate is the
    seconds of speech per word of the line, over the lines of RATE_MIN_WORDS words or more. n counts the takes with a
    level, and a measure no take of the voice has is left out.
    """
    levels: dict[str, list[float]] = {}
    rates: dict[str, list[float]] = {}
    voices: dict[str, list[list[float]]] = {}
    for k, entry in entries.items():
        voice, signal = (takes.get(k) or {}).get("voice"), entry.get("signal")
        if not voice or not current(signal, SIGNAL_V) or not isinstance(signal.get("level"), (int, float)):
            continue
        levels.setdefault(voice, []).append(signal["level"])
        count = len(expected_words(lines[int(k)], bible)) if int(k) < len(lines) else 0
        if count >= RATE_MIN_WORDS and isinstance(signal.get("active"), (int, float)):
            rates.setdefault(voice, []).append(signal["active"] / count)
        heard_voice = embedding(entry.get("speaker"))
        if heard_voice and int(k) < len(lines) and shows_timbre(lines[int(k)], signal):
            voices.setdefault(voice, []).append(heard_voice)
    centres = {voice: centre(found) for voice, found in voices.items() if len(found) >= DRIFT_MIN}
    return {voice: {"n": len(values), "level": spread(values),
                    **({"rate": spread(rates[voice])} if voice in rates else {}), **centres.get(voice, {})}
            for voice, values in sorted(levels.items())}


def centre(found: list[list[float]]) -> dict:
    """{"centre", "similarity", "m"}: the centre of a voice's embeddings and their similarity to it.

    found: the embeddings, at length 1, of the voice's takes that show its timbre.
    centre is packed as a speaker block holds an embedding. It is the mean of the embeddings, again without those
    over DRIFT_TRIM_MADS below the median similarity to the first mean. similarity is [median, median absolute
    deviation] of each embedding's similarity to the centre, and m counts the embeddings. {} when the embeddings
    sum to nothing.
    """
    def mean(vectors: list[list[float]]) -> list[float]:
        total = [sum(column) for column in zip(*vectors)]
        norm = math.sqrt(sum(v * v for v in total)) or 1.0
        return [v / norm for v in total]

    first = mean(found)
    middle, mad = spread([similarity(e, first) for e in found])
    kept = [e for e in found if similarity(e, first) >= middle - DRIFT_TRIM_MADS * max(mad, DRIFT_MAD_FLOOR)]
    point = embedding(packed(mean(kept or found)))
    if point is None:
        return {}
    return {"centre": packed(point), "similarity": spread([similarity(e, point) for e in found]), "m": len(found)}


def asks_for_sound(line: dict) -> bool:
    """Whether a script line asks for a sound after its words.

    line: the script line.
    True for a line with a vocal event in parentheses, and for a line whose direction has a word of TAIL_SOUNDS.
    """
    events = {m.strip().lower() for m in re.findall(r"\(([^()]*)\)", line.get("text") or "")}
    said = set(re.findall(r"[a-z]+", (line.get("direction") or "").lower()))
    return bool(events & VOCAL_EVENTS or said & TAIL_SOUNDS)


def shows_timbre(line: dict, signal) -> bool:
    """Whether a take of a line can show its voice's timbre.

    line: the script line.
    signal: the take's signal block, or None.
    True for a take with DRIFT_SPEECH seconds of speech or more by a signal block of SIGNAL_V, of a line without a
    direction and without a vocal event in parentheses.
    """
    active = signal.get("active") if current(signal, SIGNAL_V) else None
    return (finite(active) and active >= DRIFT_SPEECH and not str(line.get("direction") or "").strip()
            and not keeps_its_pauses(line))


def judged(entry: dict, line: dict, bible: dict, baseline: dict | None) -> dict:
    """entry with the fields judge() derives from its evidence, in place of any it held.

    entry: a take's entry with its evidence.
    line: the take's script line.
    bible: the serial's bible.
    baseline: the voice's baseline for the chapter, or None.
    """
    asr, signal = evidence(entry)
    kept = {k: entry[k] for k in KEPT if k in entry}
    return {**kept, **judge(asr, signal, voice_text(line, bible)[1], baseline, cut_pauses=not keeps_its_pauses(line),
                            offset=LEVEL_OFFSETS[direction_class(line.get("direction"))], vad=entry.get("vad"),
                            speaker=entry.get("speaker"), asr2=entry.get("asr2"), sounds=asks_for_sound(line),
                            plain=shows_timbre(line, signal), phone=entry.get("phone"),
                            said=picked_spellings(bible))}


def rejudged(entries: dict, takes: dict, lines: list[dict], bible: dict) -> dict:
    """entries with each one judged again from its evidence, by the rules as they are now.

    entries: {line: entry} from take_check.json.
    takes: {line: breeze_takes.json entry}, which names each take's voice.
    lines: the script lines.
    bible: the serial's bible.
    Returns {line: entry}. An entry without an asr block of ASR_V, or for a line the script doesn't have, is
    returned as it is. The voices' baselines come from the entries that are for their take's current audio.
    """
    voices = baselines({k: e for k, e in entries.items() if for_take(e, takes.get(k))}, takes, lines, bible)
    out = {}
    for k, entry in entries.items():
        known = isinstance(entry, dict) and current(entry.get("asr"), ASR_V) and isinstance(takes.get(k), dict)
        if known and k.isdigit() and int(k) < len(lines):
            out[k] = judged(entry, lines[int(k)], bible, voices.get(takes[k].get("voice")))
        else:
            out[k] = entry
    return out


def for_take(entry, take) -> bool:
    """Whether a take_check.json entry is for the audio a breeze_takes.json entry has now."""
    return (isinstance(entry, dict) and isinstance(take, dict) and entry.get("key") == take.get("key")
            and entry.get("audio") == take.get("audio"))


def join_cuts(folder: Path, takes: dict, lines: list[dict], bible: dict, *, stored: bool = False) -> dict:
    """The cuts the join applies to the chapter's saved takes: the long pauses and the tails.

    folder: the chapter folder, which holds take_check.json.
    takes: {line: breeze_takes.json entry}.
    lines: the script lines.
    bible: the serial's bible.
    stored: take the cuts as take_check.json holds them unless stale() calls for judging the evidence again.
    Returns {line: [[start, end], ...]} in seconds of each take's file, for the takes that have a cut. The cuts come
    from the evidence saved for a take's current audio, judged by the rules as they are now, and a take without both
    blocks in the current versions doesn't get any. Never raises.
    """
    try:
        entries = saved_entries(folder, takes)
        path = folder / CHECK_FILE
        if not stored or not path.is_file() or stale(lib.load_json(path), bible):
            entries = rejudged(entries, takes, lines, bible)
        return {k: e["repairs"]["cuts"] for k, e in entries.items()
                if current(e.get("asr"), ASR_V) and current(e.get("signal"), SIGNAL_V)
                and isinstance(e.get("repairs"), dict) and e["repairs"].get("cuts")}
    except Exception:
        return {}


def ends_a_take(cut: list, entry: dict) -> bool:
    """Whether a cut runs to the end of its take's file, by the length the entry's signal block measured."""
    seconds = entry.get("signal", {}).get("seconds") if isinstance(entry.get("signal"), dict) else None
    return isinstance(cut, list) and len(cut) == 2 and finite(cut[1]) and finite(seconds) and cut[1] >= seconds - 0.02


def saved_entries(folder: Path, takes: dict) -> dict:
    """The entries of the chapter folder's take_check.json that are for the takes' current audio.

    folder: the chapter folder.
    takes: {line: breeze_takes.json entry}.
    Returns {line: entry}. {} when the file is missing or does not parse.
    """
    path = folder / CHECK_FILE
    try:
        old = (lib.load_json(path).get("takes") or {}) if path.is_file() else {}
        return {k: e for k, e in old.items() if for_take(e, takes.get(k))}
    except (OSError, ValueError, AttributeError, RecursionError):
        return {}


def write_check(path: Path, data: dict) -> None:
    """Writes take_check.json with each take's entry on one line.

    path: the file.
    data: the file's content, with the entries by line under "takes".
    """
    def pair(key: str, value, pad: str) -> str:
        return f"{pad}{json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}"

    parts = [pair(k, v, "  ") for k, v in data.items() if k != "takes"]
    takes = ",\n".join(pair(k, v, "    ") for k, v in (data.get("takes") or {}).items())
    parts.append('  "takes": {\n' + takes + "\n  }" if takes else '  "takes": {}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{\n" + ",\n".join(parts) + "\n}\n", encoding="utf-8")


def check_entries(takes: dict, lines: list[dict], bible: dict, *, engine: str, comfy_url: str, log,
                  folder: Path | None = None, saved: dict | None = None) -> dict:
    """The take_check.json entry for each take, from hearing it now.

    takes: {line: breeze_takes.json entry} of the takes to hear.
    lines: the script lines.
    bible: the serial's bible.
    engine, comfy_url, log: as hear takes them.
    folder, saved: the chapter folder and all of the chapter's takes by line. With them, each take is judged against
        its voice's baseline over the chapter, from these takes and the entries take_check.json holds for the others.
    Returns {line: entry}. With folder, a take whose saved entry lacks only blocks of EXTRA_V gets only those, and
    keeps its other blocks. Raises ComfyError when the recognizer cannot run here or fails.
    """
    keep = saved_entries(folder, takes) if folder is not None else None
    found = hear([(k, takes[k]) for k in sorted(takes, key=int)], engine, comfy_url, log, keep=keep)
    voices, voice_of = {}, {k: t.get("voice") for k, t in takes.items()}
    if folder is not None and saved is not None:
        voice_of = {k: v or (saved.get(k) or {}).get("voice") for k, v in voice_of.items()}
        every = {**saved, **{k: {**t, "voice": voice_of[k]} for k, t in takes.items()}}
        voices = baselines({**saved_entries(folder, saved), **found}, every, lines, bible)
    return {k: judged(e, lines[int(k)], bible, voices.get(voice_of[k])) for k, e in found.items()}


def save_entries(folder: Path, entries: dict, engine: str, takes: dict | None = None, lines: list[dict] | None = None,
                 bible: dict | None = None) -> None:
    """Writes entries into the chapter folder's take_check.json and keeps its entries for other lines.

    folder: the chapter folder.
    entries: {line: entry} to write.
    engine: the recognizer that heard them.
    takes, lines, bible: all of the chapter's takes by line, the script lines and the bible. With them the file also
        gets each voice's baseline, under "voices".
    The file keeps the rules and the picked spellings its other entries were judged with, so a file that mixes old
    verdicts with these stays one that stale() sends to judging again.
    """
    path = folder / CHECK_FILE
    old = lib.load_json(path) if path.is_file() else {}
    merged = {**(old.get("takes") or {}), **entries}
    data = {"at": lib.now_iso(), "model": model_name(engine), "asr": engine}
    if old.get("rules") == JUDGE_V or not old.get("takes"):
        data["rules"] = JUDGE_V
    before = old.get("names") if isinstance(old.get("names"), dict) else {}
    held = picked_spellings(bible) if bible is not None else before
    data["names"] = held if not old.get("takes") or before == held else before
    if takes is not None and lines is not None and bible is not None:
        fresh = {k: e for k, e in merged.items() if isinstance(takes.get(k), dict)
                 and e.get("key") == takes[k].get("key") and e.get("audio") == takes[k].get("audio")}
        data["voices"] = baselines(fresh, takes, lines, bible)
    elif isinstance(old.get("voices"), dict):
        data["voices"] = old["voices"]
    write_check(path, {**data, "takes": merged})


def voice_findings(entries: dict, takes: dict) -> list[dict]:
    """The voices whose takes one rule flags so often that the cause is the voice and not a take.

    entries: {line: entry} of the chapter's judged takes.
    takes: {line: breeze_takes.json entry}, which names each take's voice.
    Returns [{"voice", "reason", "lines", "of"}, ...] for each voice and reason that flags BREAKER_MIN of the
    voice's takes or more and over BREAKER_SHARE of them. lines are the flagged lines and of counts the voice's
    judged takes. The reason "name" gives no finding.
    """
    counted: dict[str, int] = {}
    hits: dict[tuple[str, str], list[int]] = {}
    for k, entry in entries.items():
        voice = (takes.get(k) or {}).get("voice")
        if not voice or entry.get("verdict") not in VERDICTS:
            continue
        counted[voice] = counted.get(voice, 0) + 1
        if entry["verdict"] == "flagged":
            for reason in entry.get("reasons") or []:
                if reason != "name":
                    hits.setdefault((voice, reason), []).append(int(k))
    return [{"voice": voice, "reason": reason, "lines": sorted(found), "of": counted[voice]}
            for (voice, reason), found in sorted(hits.items())
            if len(found) >= BREAKER_MIN and len(found) > BREAKER_SHARE * counted[voice]]


def summarize(results: dict, takes: dict | None = None) -> dict:
    """The render.check record for a set of per-take results.

    results: {line: take_check.json entry}.
    takes: the chapter's breeze_takes.json entries. With them, the record lists under "retaken" the lines whose
        take replaced a flagged one.
    """
    out = {"at": lib.now_iso(), "file": CHECK_FILE, "checked": len(results)}
    for verdict in VERDICTS[1:]:
        out[verdict] = sorted(int(k) for k, r in results.items() if r["verdict"] == verdict)
    retaken = sorted(int(k) for k, t in (takes or {}).items() if t.get("retaken") and k in results)
    if retaken:
        out["retaken"] = retaken
    return out


def describe(summary: dict) -> str:
    """One log line for a check summary."""
    if summary.get("skipped"):
        return f"take check skipped: {summary['skipped']}"
    flagged = summary.get("flagged") or []
    bits = []
    if flagged:
        bits.append(f"{len(flagged)} of {summary['checked']} takes flagged (lines {', '.join(map(str, flagged))}), listen to them")
    else:
        bits.append(f"no takes flagged in {summary['checked']}")
    if summary.get("suspect"):
        bits.append(f"{len(summary['suspect'])} suspect, usually name spellings")
    if summary.get("unreadable"):
        bits.append(f"{len(summary['unreadable'])} unreadable")
    if summary.get("retaken"):
        bits.append(f"{len(summary['retaken'])} taken again after a flag")
    return "take check: " + ", ".join(bits)


def chapter_evidence(slug: str, number: int, *, engine: str, comfy_url: str, log, redo: bool = False,
                     listen: bool = True) -> tuple[dict, dict, list[dict], dict, int]:
    """The evidence for every take of a chapter, reusing what take_check.json holds for the same audio.

    slug: the serial.
    number: the chapter.
    engine, comfy_url, log: as hear takes them.
    redo: hear every take again.
    listen: hear the takes whose saved evidence is missing or out of date. Without it they are left out, and saved
        evidence counts whichever recognizer heard it.
    Returns (entries, takes, lines, bible, heard): the entries by line without their derived fields, the chapter's
    takes, its script lines, the bible, and how many takes were heard now. Raises ComfyError when the chapter has
    no Breeze takes, or the recognizer cannot run here or fails.
    """
    meta, _ = lib.load_chapter(slug, number)
    folder = lib.chapter_dir(slug, number)
    render = meta.get("render") if isinstance(meta.get("render"), dict) else {}
    if render.get("engine") != "breeze" or not (folder / TAKES_FILE).is_file():
        raise ComfyError(f"chapter {number} has no Breeze takes to check")
    lines = lib.load_json(folder / "script.json")["lines"]
    bible = lib.load_bible(slug)
    takes = lib.load_json(folder / TAKES_FILE)["takes"]
    previous = {} if redo else saved_entries(folder, takes)
    extras = extra_blocks() if listen and engine == "parakeet" else set()
    entries, todo = {}, []
    for k in sorted(takes, key=int):
        old = previous.get(k) or {}
        heard_by = engine if listen or not isinstance(old.get("asr"), dict) else old["asr"].get("engine")
        if reusable(old, takes[k], heard_by, extras):
            entries[k] = {f: old[f] for f in KEPT if f in old}
        else:
            todo.append((k, takes[k]))
    if listen:
        log(f"   checking {len(todo)} takes with {LABELS[engine]}"
            + (f", {len(entries)} already checked" if entries else "") + "...")
        entries.update(hear(todo, engine, comfy_url, log, keep=previous))
    return entries, takes, lines, bible, len(todo) if listen else 0


def check_takes(slug: str, number: int, *, comfy_url: str = "http://127.0.0.1:8188", log=print,
                redo: bool = False, asr: str | None = None) -> dict:
    """Hears the chapter's Breeze takes and records which ones do not say their line.

    slug: the serial.
    number: the chapter.
    comfy_url: the ComfyUI that runs Qwen3-ASR.
    log: receives the progress lines.
    redo: hear every take again instead of reusing the saved evidence of an unchanged take.
    asr: "qwen3" or "parakeet". None uses this machine's recognizer, from asr.choose.
    Returns the summary, which is also stored as render.check in chapter.yaml. Saved evidence is reused only when
    the take, its audio, the recognizer and the block versions are unchanged, and every take is judged again from
    its evidence. Raises ComfyError when the chapter has no Breeze takes, the recognizer cannot run here, a take
    file lies outside the ComfyUI output folder for Qwen3-ASR, or the recognizer fails.
    """
    # Gather
    engine = choose(asr)
    entries, takes, lines, bible, _ = chapter_evidence(slug, number, engine=engine, comfy_url=comfy_url, log=log,
                                                       redo=redo)

    # Judge
    voices = baselines(entries, takes, lines, bible)
    results = {k: judged(e, lines[int(k)], bible, voices.get(takes[k].get("voice"))) for k, e in entries.items()}

    # Record
    summary = summarize(results, takes)
    write_check(lib.chapter_dir(slug, number) / CHECK_FILE,
                {"at": summary["at"], "model": model_name(engine), "asr": engine, "rules": JUDGE_V,
                 "names": picked_spellings(bible), "voices": voices,
                 "takes": results})
    meta, _ = lib.load_chapter(slug, number)
    meta["render"]["check"] = summary
    lib.save_chapter_meta(slug, number, meta)
    log("   " + describe(summary))
    return summary


def check_after_render(slug: str, number: int, *, comfy_url: str, log=print, asr: str | None = None) -> dict:
    """Runs check_takes for a chapter that just rendered. Never raises: a failure is logged and recorded as skipped."""
    try:
        return check_takes(slug, number, comfy_url=comfy_url, log=log, asr=asr)
    except Exception as e:
        summary = {"at": lib.now_iso(), "skipped": str(e) if isinstance(e, ComfyError) else f"{type(e).__name__}: {e}"}
        meta, _ = lib.load_chapter(slug, number)
        if isinstance(meta.get("render"), dict):
            meta["render"]["check"] = summary
            lib.save_chapter_meta(slug, number, meta)
        log("   " + describe(summary))
        return summary
