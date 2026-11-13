"""What a take's phones say about a name, for the take check's name rule, the screening of a spelling and the
Names tab.

A phone string is the IPA a phoneme recognizer decodes from audio, one character per symbol. A name's span in a take
is a pair: the phones inside the name's time span, and the window around it that holds them.

The name rule and the screening read skeletons: a name's consonants in order, with one VOWEL for each run of
vowels. The reference is the skeleton of the spelling the voice is sent. adds() gives the sounds that both the word
recognizer's spelling and the phone recognizer's phones add to it.

The Names tab sorts a name's sayings by the distance between phone strings. There a reference is compared with the
window and may match any part of it. Standard library only.
"""
from __future__ import annotations

import re

# vowels as (height from 0 open to 1 close, backness from 0 front to 1 back, rounded)
VOWELS = {"i": (1.0, 0.0, 0), "y": (1.0, 0.0, 1), "ɨ": (1.0, 0.5, 0), "ʉ": (1.0, 0.5, 1), "ɯ": (1.0, 1.0, 0),
          "u": (1.0, 1.0, 1), "ɪ": (0.85, 0.2, 0), "ʏ": (0.85, 0.2, 1), "ʊ": (0.85, 0.8, 1), "e": (0.7, 0.0, 0),
          "ø": (0.7, 0.0, 1), "ɘ": (0.7, 0.5, 0), "ɵ": (0.7, 0.5, 1), "ɤ": (0.7, 1.0, 0), "o": (0.7, 1.0, 1),
          "ə": (0.5, 0.5, 0), "ɛ": (0.35, 0.0, 0), "œ": (0.35, 0.0, 1), "ɜ": (0.35, 0.5, 0), "ɞ": (0.35, 0.5, 1),
          "ʌ": (0.35, 1.0, 0), "ɔ": (0.35, 1.0, 1), "æ": (0.15, 0.0, 0), "ɐ": (0.15, 0.5, 0), "a": (0.0, 0.2, 0),
          "ɶ": (0.0, 0.0, 1), "ɑ": (0.0, 1.0, 0), "ɒ": (0.0, 1.0, 1)}
# consonants as (place from 0 at the lips to 1 at the glottis, manner, voiced)
CONSONANTS = {"p": (0.0, "stop", 0), "b": (0.0, "stop", 1), "t": (0.4, "stop", 0), "d": (0.4, "stop", 1),
              "k": (0.8, "stop", 0), "g": (0.8, "stop", 1), "ʔ": (1.0, "stop", 0), "ɾ": (0.4, "stop", 1),
              "f": (0.1, "fricative", 0), "v": (0.1, "fricative", 1), "θ": (0.3, "fricative", 0),
              "ð": (0.3, "fricative", 1), "s": (0.4, "fricative", 0), "z": (0.4, "fricative", 1),
              "ʃ": (0.5, "fricative", 0), "ʒ": (0.5, "fricative", 1), "ç": (0.7, "fricative", 0),
              "x": (0.8, "fricative", 0), "h": (1.0, "fricative", 0), "ɦ": (1.0, "fricative", 1),
              "m": (0.0, "nasal", 1), "n": (0.4, "nasal", 1), "ɲ": (0.7, "nasal", 1), "ŋ": (0.8, "nasal", 1),
              "l": (0.4, "liquid", 1), "r": (0.4, "liquid", 1), "ɹ": (0.45, "liquid", 1), "ɻ": (0.5, "liquid", 1),
              "ʁ": (0.9, "liquid", 1), "ʀ": (0.9, "liquid", 1), "ʋ": (0.1, "glide", 1), "j": (0.7, "glide", 1),
              "ɥ": (0.7, "glide", 1), "w": (0.85, "glide", 1)}
GLIDE_VOWELS = {"j": "i", "ɥ": "y", "w": "u"}
RHOTIC = frozenset(("ɹ", "˞", "ɜ", "ə"))
# the marks that follow a symbol: aspiration, length, rhoticity and the combining diacritics
MARKS = frozenset("ʰʲʷʼː˞ˠˤ") | frozenset(chr(c) for c in (0x303, 0x31A, 0x325, 0x329, 0x32A, 0x334, 0x33A))
MARK_COST = 0.3
WEAK = {"ə": 0.6, "ʔ": 0.5, "h": 0.7, "ɪ": 0.8, "ʊ": 0.8}
# the most phones a reference loses at its ends, and the share of an end phone's cost that a window may add for it
# and still count as having it
TRIM_MOST = 6
TRIM_HAS = 0.5
# the fewest phones a reference has to have for a window to be held to it
MIN_REF = 3
CONSENSUS_SAMPLE = 60
# a run of vowels in a skeleton
VOWEL = "V"
VOWEL_LETTERS = "aeiou"
SIBILANTS = frozenset("szʃʒ")
DIGRAPHS = {"th": "θ", "sh": "ʃ", "ch": "ʃ", "ph": "f", "ck": "k", "ng": "ŋ", "qu": "kw"}
LETTERS = {"b": "b", "d": "d", "f": "f", "g": "g", "j": "ʒ", "k": "k", "l": "l", "m": "m", "n": "n", "p": "p",
           "q": "k", "r": "ɹ", "s": "s", "t": "t", "v": "v", "x": "ks", "z": "z"}
# how near two added consonants have to be, by sub(), to be one sound that both recognizers heard
SAME = 0.5
# a heard consonant over this far from a reference consonant, by sub(), doesn't stand for it
FAR = 0.6


def indel(p: str) -> float:
    """The cost of a phone that one string has and the other lacks.

    p: the phone.
    Returns MARK_COST for a mark, the phone's cost in WEAK when it has one, and else 1.0.
    """
    return MARK_COST if p in MARKS else WEAK.get(p, 1.0)


def sub(a: str, b: str) -> float:
    """The cost of hearing one phone as another.

    a, b: the phones.
    Returns 0.0 for the same phone, less than 1.0 for two vowels or two consonants that are near each other, for a
    glide and its vowel, and for two rhotic sounds, and 1.0 for unrelated ones.
    """
    if a == b:
        return 0.0
    if a in MARKS or b in MARKS:
        return MARK_COST if a in MARKS and b in MARKS else 0.4 if {a, b} <= RHOTIC else 1.0
    if a in VOWELS and b in VOWELS:
        (h1, b1, r1), (h2, b2, r2) = VOWELS[a], VOWELS[b]
        return min(1.0, 0.15 + 0.9 * abs(h1 - h2) + 0.5 * abs(b1 - b2) + 0.2 * (r1 != r2))
    if a in CONSONANTS and b in CONSONANTS:
        (p1, m1, v1), (p2, m2, v2) = CONSONANTS[a], CONSONANTS[b]
        return min(1.0, 0.15 + 0.25 * (v1 != v2) + 0.45 * (m1 != m2) + 1.2 * abs(p1 - p2))
    for glide, vowel in GLIDE_VOWELS.items():
        other = b if a == glide else a if b == glide else None
        if other in VOWELS:
            return min(1.0, 0.3 + sub(vowel, other))
    return 0.4 if {a, b} <= RHOTIC else 1.0


def weight(phones: str) -> float:
    """The cost of saying none of a phone string.

    phones: the phone string.
    Returns the sum of indel() over its phones, and 1.0 for an empty string.
    """
    return sum(indel(p) for p in phones) or 1.0


def within(ref: str, window: str) -> tuple[float, int, int]:
    """The part of a window that is nearest a reference.

    ref: the reference phones.
    window: the phones to look in.
    Returns (cost, start, end): the least cost of turning ref into window[start:end], over every part of window.
    """
    prev = [(0.0, j) for j in range(len(window) + 1)]
    for a in ref:
        gone = indel(a)
        row = [(prev[0][0] + gone, 0)]
        for j, b in enumerate(window, 1):
            row.append(min((prev[j - 1][0] + sub(a, b), prev[j - 1][1]), (prev[j][0] + gone, prev[j][1]),
                           (row[j - 1][0] + indel(b), row[j - 1][1])))
        prev = row
    end = min(range(len(window) + 1), key=lambda j: prev[j][0])
    return prev[end][0], prev[end][1], end


def apart(ref: str, window: str) -> float:
    """How far a window is from holding a reference.

    ref: the reference phones.
    window: the phones to look in.
    Returns the least cost from within() as a share of ref's weight: 0.0 when a part of window is ref, and about 1.0
    when no part of it is like ref.
    """
    return round(within(ref, window)[0] / weight(ref), 3)


def whole(a: str, b: str) -> float:
    """How far apart two phone strings are, end to end.

    a, b: the phone strings.
    Returns the cost of turning one into the other as a share of the heavier one's weight.
    """
    prev = [0.0]
    for y in b:
        prev.append(prev[-1] + indel(y))
    for x in a:
        gone = indel(x)
        row = [prev[0] + gone]
        for j, y in enumerate(b, 1):
            row.append(min(prev[j - 1] + sub(x, y), prev[j] + gone, row[j - 1] + indel(y)))
        prev = row
    return prev[-1] / max(weight(a), weight(b))


def consensus(spans: list, sample: int = CONSENSUS_SAMPLE) -> str | None:
    """The reference for a name: how its sayings usually sound.

    spans: [inside, window] for each saying, as takecheck.name_spans gives them.
    sample: the most sayings to read, thinned evenly from spans.
    Returns the inside, among those of the median length or longer, that is nearest the others end to end, without
    the phones at its ends that fewer than half of the windows have there. A window has an end phone when the
    reference costs it less than TRIM_HAS of the phone's cost more with the phone than without. A reference of
    MIN_REF phones or fewer loses none, and none loses more than TRIM_MOST. None when no saying has phones inside.
    The same spans give the same reference.
    """
    found = [(str(s[0]), str(s[1])) for s in spans if isinstance(s, (list, tuple)) and len(s) >= 2 and s[0]]
    if not found:
        return None
    if len(found) > sample:
        found = [found[i * len(found) // sample] for i in range(sample)]
    insides = [inside for inside, _ in found]
    windows = [window for _, window in found]
    typical = sorted(len(inside) for inside in insides)[len(insides) // 2]
    ref = min({inside for inside in insides if len(inside) >= typical},
              key=lambda a: (sum(whole(a, b) for b in insides), a))
    for _ in range(TRIM_MOST):
        if len(ref) <= MIN_REF:
            break
        costs = [within(ref, window)[0] for window in windows]
        drop = None
        for form, phone in ((ref[1:], ref[0]), (ref[:-1], ref[-1])):
            has = sum(1 for window, cost in zip(windows, costs)
                      if cost - within(form, window)[0] < TRIM_HAS * indel(phone))
            if 2 * has < len(windows) and (drop is None or has < drop[1]):
                drop = (form, has)
        if drop is None:
            break
        ref = drop[0]
    return ref


def ways(spans: list, ref: str | None, limit: float) -> list[dict]:
    """The different ways a name was said, the most frequent first.

    spans: [inside, window] for each saying.
    ref: the name's reference, which leads the first way, or None for the consensus of the spans.
    limit: how far a window may be from a way's phones and still be that way.
    Returns [{"phones", "count"}, ...]. Each saying counts for the first way whose phones its window holds within
    limit, and a saying no way holds starts a way of its own with its inside. The first way is there with a count
    of 0 when no saying is it. [] without a reference and without a saying that has phones.
    """
    found = [(str(s[0]), str(s[1])) for s in spans if isinstance(s, (list, tuple)) and len(s) >= 2 and s[1]]
    ref = ref or consensus(found)
    if not ref:
        return []
    out = [{"phones": ref, "count": 0}]
    for inside, window in found:
        way = next((w for w in out if apart(w["phones"], window) <= limit), None)
        if way is None and inside:
            way = {"phones": inside, "count": 0}
            out.append(way)
        if way is not None:
            way["count"] += 1
    return [out[0]] + sorted(out[1:], key=lambda w: -w["count"])


def push(out: list, symbol: str) -> bool:
    """Adds a symbol to a skeleton unless it repeats the last one. Returns whether it was added."""
    if symbol and (not out or out[-1] != symbol):
        out.append(symbol)
        return True
    return False


def spelled(text: str, loud: bool = False) -> tuple[list[str], list[int]]:
    """The skeleton of a spelling, read as English.

    text: one or more words.
    loud: whether the last word's final e after a consonant is a vowel. It is silent otherwise, as every other
        word's is.
    Returns (symbols, words): the consonants in order with one VOWEL for each run of vowels, and for each symbol the
    number of the word it is from. A w or a y after a vowel is part of the vowel, and an h is said only before a
    vowel.
    """
    out: list[str] = []
    owner: list[int] = []
    found = re.findall(r"[a-z]+", text.lower().replace("'", "").replace("’", ""))
    for number, word in enumerate(found):
        if loud and number == len(found) - 1 and word[-1] == "e":
            pass
        elif len(word) > 2 and word[-1] == "e" and word[-2] not in "aeiouy" and re.search(r"[aeiouy]", word[:-2]):
            word = word[:-1]
        elif (len(word) > 3 and word.endswith("es") and word[-3] not in "aeiouyszx"
              and re.search(r"[aeiouy]", word[:-3])):
            word = word[:-2] + "s"
        symbols: list[str] = []
        i = 0
        while i < len(word):
            c, two = word[i], word[i:i + 2]
            after = word[i + 1:i + 2]
            if c in VOWEL_LETTERS:
                symbols.append(VOWEL)
            elif c == "y":
                symbols.append("j" if i == 0 and after and after in VOWEL_LETTERS else VOWEL)
            elif c == "w":
                if two == "wh":
                    symbols.append("w")
                    i += 1
                elif two == "wr" and i == 0:
                    symbols.append("ɹ")
                    i += 1
                elif not (i and word[i - 1] in "aeiouyw"):
                    symbols.append("w")
            elif c == "h":
                if after and after in "aeiouy":
                    symbols.append("h")
            elif two == "gh":
                if i == 0:
                    symbols.append("g")
                i += 1
            elif two == "kn" and i == 0:
                symbols.append("n")
                i += 1
            elif two in DIGRAPHS:
                symbols.extend(DIGRAPHS[two])
                i += 1
            elif c == "c":
                symbols.append("s" if after and after in "eiy" else "k")
            elif c in LETTERS:
                symbols.extend(LETTERS[c])
            i += 1
        for symbol in symbols:
            if push(out, symbol):
                owner.append(number)
    return out, owner


def sounded(phones: str) -> list[str]:
    """The skeleton of a phone string.

    phones: the phones, one character each.
    Returns the consonants in order with one VOWEL for each run of vowels. A glide right after a vowel is part of
    the vowel, and a glottal stop and the marks are left out.
    """
    out: list[str] = []
    for p in phones:
        if p in VOWELS:
            push(out, VOWEL)
        elif p in GLIDE_VOWELS:
            if not (out and out[-1] == VOWEL):
                push(out, p)
        elif p in CONSONANTS and p != "ʔ":
            push(out, p)
    return out


def stands(a: str, b: str) -> float:
    """The cost of a heard skeleton symbol standing for a reference symbol.

    a, b: the symbols.
    Returns 0.0 for the same symbol, sub() for two consonants within FAR of each other, and more than a symbol
    added and a symbol dropped cost together for any other pair.
    """
    if a == VOWEL or b == VOWEL:
        return 0.0 if a == b else 9.0
    near = sub(a, b)
    return near if near <= FAR else 9.0


def aligned(ref: list[str], heard: list[str]) -> list[tuple[int | None, int | None]]:
    """The cheapest way to lay a heard skeleton on a reference, as early in heard as the cost allows.

    ref, heard: the skeletons.
    Returns [(reference index or None, heard index or None), ...] in order.
    """
    n, m = len(ref), len(heard)
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        best[i][0] = float(i)
    for j in range(1, m + 1):
        best[0][j] = float(j)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best[i][j] = min(best[i - 1][j - 1] + stands(ref[i - 1], heard[j - 1]), best[i - 1][j] + 1.0,
                             best[i][j - 1] + 1.0)
    out, i, j = [], n, m
    while i > 0 or j > 0:
        if j > 0 and abs(best[i][j] - (best[i][j - 1] + 1.0)) < 1e-9:
            out.append((None, j - 1))
            j -= 1
        elif i > 0 and j > 0 and abs(best[i][j] - (best[i - 1][j - 1] + stands(ref[i - 1], heard[j - 1]))) < 1e-9:
            out.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        else:
            out.append((i - 1, None))
            i -= 1
    return out[::-1]


def added(heard: list[str], pairs: list[tuple[int | None, int | None]]) -> list[tuple[str, int, int]]:
    """The sounds a heard skeleton has and its reference lacks.

    heard: the heard skeleton.
    pairs: its alignment with the reference, from aligned().
    Returns [(symbol, gap, heard index), ...] in heard's order, where gap counts the reference symbols before the
    sound.
    """
    out, gap = [], 0
    for i, j in pairs:
        if i is None:
            out.append((heard[j], gap, j))
        else:
            gap = i + 1
    return out


def forms(said: str) -> list[list[str]]:
    """The reference skeletons of a name as the voice is sent it.

    said: the name's words in the line, which end in 's or s' for a possessive.
    Returns the skeleton of the name, and for a possessive also the skeleton with its z, and with a vowel before
    the z after a hissing sound.
    """
    text = said.lower().replace("’", "'")
    owned = text.endswith("'s") or text.endswith("s'")
    base = spelled(text[:-2] if text.endswith("'s") else text[:-1] if text.endswith("s'") else text)[0]
    out = [base]
    if owned:
        if base[-1:] != ["z"]:
            out.append(base + ["z"])
        if base and base[-1] in SIBILANTS:
            out.append(base + [VOWEL, "z"])
    return out


def by_spelling(ref: list[str], heard: str, loud: bool = False) -> list[tuple[str, int]]:
    """The sounds a spelling adds to a reference.

    ref: the reference skeleton.
    heard: the words the word recognizer wrote in the name's place.
    loud: whether a final e of the spelling is a vowel.
    Returns [(symbol, gap), ...]. A first or last heard word adds nothing when none of its sounds stands for a
    sound of the name and the other words have the name's sound at that end.
    """
    symbols, owner = spelled(heard, loud)
    while True:
        pairs = aligned(ref, symbols)
        words = sorted(set(owner))
        standing = {owner[j] for i, j in pairs if i is not None and j is not None}
        stood = {i for i, j in pairs if i is not None and j is not None}
        drop = None
        if len(words) > 1 and ref:
            if words[0] not in standing and 0 in stood:
                drop = words[0]
            elif words[-1] not in standing and len(ref) - 1 in stood:
                drop = words[-1]
        if drop is None:
            return [(s, g) for s, g, _ in added(symbols, pairs)]
        keep = [k for k, w in enumerate(owner) if w != drop]
        symbols, owner = [symbols[k] for k in keep], [owner[k] for k in keep]


def by_sound(ref: list[str], phones: str) -> list[tuple[str, int]]:
    """The sounds a phone string adds to a reference, inside the name.

    ref: the reference skeleton.
    phones: the phones in and around the name's time span.
    Returns [(symbol, gap), ...]: the added sounds between the first and the last heard sound that stands for a
    sound of the name, a consonant right before the first, and a vowel right after the last. Other sounds before
    and after belong to the neighbouring words. [] when no heard sound stands for a sound of the name.
    """
    heard = sounded(phones)
    pairs = aligned(ref, heard)
    stood = [j for i, j in pairs if i is not None and j is not None]
    if not stood:
        return []
    first, last = min(stood), max(stood)
    return [(symbol, gap) for symbol, gap, j in added(heard, pairs)
            if first < j < last or (j == first - 1 and symbol != VOWEL) or (j == last + 1 and symbol == VOWEL)]


def shared(one: list[tuple[str, int]], two: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """The added sounds that two hearings agree on.

    one, two: the added sounds of each, as (symbol, gap).
    Returns the sounds of one that two has too: a vowel at the same gap, or a consonant within SAME of one of two's
    that is at most one gap away.
    """
    out = []
    for symbol, gap in one:
        if symbol == VOWEL:
            if (VOWEL, gap) in two:
                out.append((symbol, gap))
        elif any(other != VOWEL and abs(gap - at) <= 1 and sub(symbol, other) <= SAME for other, at in two):
            out.append((symbol, gap))
    return out


def adds(said: str, heard: str, phones: str, following: str = "", before: str = "") -> list[tuple[str, int]]:
    """The sounds a saying adds to its name, by both recognizers.

    said: the name's words as the voice was sent them.
    heard: the words the word recognizer wrote in the name's place.
    phones: the phones the phone recognizer heard in and around the saying's time span.
    following: the line's next word, or "" at the end of the line.
    before: the line's word before the name, or "" at the start of the line.
    Returns [(symbol, gap), ...]: the added sounds that the spelling and the phones share, against the form of the
    name that leaves the fewest. A final e of the spelling is a vowel only where the vowel the phones have after
    the name can't be the next word's: not in a possessive, and not before a word that starts with a vowel, an h
    or the name's last sound, or that has no letters, as a number has none. A consonant before the name that the
    word before ends in is that word's. [] for a name whose spelling has no sound.
    """
    refs = [ref for ref in forms(said) if ref]
    if not refs:
        return []
    onset = spelled(following)[0][:1]
    coda = spelled(before)[0][-1:]
    loud = len(refs) == 1 and (not following or (bool(onset) and onset[0] not in (VOWEL, "h")
                                                 and stands(refs[0][-1], onset[0]) > FAR))
    found = []
    for ref in refs:
        both = shared(by_spelling(ref, heard, loud), by_sound(ref, phones))
        found.append([(s, g) for s, g in both
                      if not (g == 0 and s != VOWEL and coda and coda[0] != VOWEL and stands(coda[0], s) <= FAR)])
    return min(found, key=len)
