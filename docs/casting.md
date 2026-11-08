# Casting

`README.md`, step 3 of the pipeline, describes how `cast` works. There are two engines, chosen by `cast_engine` in
`serial.yaml` or `cast --engine`:

* `breeze`, the default since 2026-11-08: breeze-server designs each candidate from the brief with no reference clip. There is no Drama 3 voice file, so the voice can't be used for a Drama 3 render.
* `qwen`: Qwen3-TTS VoiceDesign in ComfyUI designs the candidates, and each is also encoded as a Drama 3 voice.

Each candidate is transcribed and compared with its sample line, and the best match is filed with status `auto`
until AJ listens and runs `pick-voice`.

---

## Writing a brief

* Name an age older than the character's age in the story: late twenties for a 20-year-old, mid fifties for a late-forties empress. Name a low register and chest resonance. Together these dropped pitch 20 to 40 percent in the 2026-11-05 recast, after AJ found the first cast too young.
* The casting pass still writes young characters as early or mid twenties. Override its brief with `--instruct` when it does.
* "Theatrical" and "dramatic swells" push the pitch up, even next to "baritone". So do "bright" and "easy laugh".
* "Genderless" comes out as a young woman, and Qwen VoiceDesign can't make an inhuman voice. The Qwen Keeper of the Throne used a post-render whisper effect instead, at voice level 0.35. `voice-effect <slug> <voice> whisper` sets one in `voice_effects` in `serial.yaml`, and `--clear` removes it.
* Breeze voice design whispers only when the brief makes the whisper the voice itself. On 2026-11-08 "speaks only in a hushed, breathy whisper, with no voiced tone at all" and "a rasping half-whisper" each gave one true whisper in four candidates. The Keeper's approved brief, which names "a dry, rasping whisper" among other things, gave none.
* A Breeze clone of a whispered clip drifts back toward a full voice on about half its lines. A direction of "a hushed, breathy whisper" on every line holds the whisper. Breeze rendering has no per-voice direction, so the 2026-11-08 trial wrote it into a scratch script.
* AJ turned down "weathered, gravelly" for a church leader and wanted a performative voice.

## Screening before AJ listens

* The median F0 of each candidate clip, from librosa.pyin with fmin 60 and fmax 500 under the audio Python, is a quick check on pitch and on how far apart two voices sit.
* The share of speech frames that pyin calls voiced tells a whisper from a voice. Normal speech scores about 0.65, the Keeper's Qwen clip with the whisper effect 0.14, and a true Breeze whisper 0.11 to 0.13.
* The casting check files the candidate whose transcript matches best. For a whisper brief that is the most voiced candidate, so pick a whispered one by seed.
* Use a new seed range for each round, so `pick-voice --seed` names exactly one candidate. Used so far: 1000, 4000, 5000, 6000, 7000, 9000, 10000, 11000, 12000 in a throwaway serial, and 13000, 13100, 13200 and 13300 in a scratch copy. The next round should start at 14000.

---

## The Hundred Reigns cast

Median F0 of the filed clips, measured from 2026-11-05 to 2026-11-08, and each voice's status in the library on
containers on 2026-11-08:

| Voice | F0 | Status |
|---|---|---|
| keeper-of-the-throne | 64 Hz | approved |
| narrator | 81 Hz | approved |
| simon | 105 Hz | approved |
| thalas | 108 Hz | approved |
| euphemia | 132 Hz | approved |
| louis | 149 Hz | approved |
| lauriane | 150 Hz | approved |
| mastemo | 155 Hz | approved |
| agnes-firewand | 208 Hz | approved |
| maublanc-paimon | 94 Hz | auto |
| adrien-adrissant | 95 Hz | auto |
| leonard-decarabia | 110 Hz | auto |
| casval | 120 Hz | auto |
| unnamed-minister | 129 Hz | auto |
| cassandra | 142 Hz | auto |
| shabram-gremory | 157 Hz | auto |
| antonine-de-shax | 172 Hz | auto |
| meredith-dose | 189 Hz | auto |
| tiella-decarabia | 190 Hz | auto |
| anna-paimon | 195 Hz | auto |
| gourmand | unknown | auto |
| unnamed-branded-voice | unknown | auto |

A voice with status `auto` was filed by the casting check, and AJ hasn't approved it. keeper-of-the-throne and
lauriane are Breeze voices since 2026-11-08, and the rest are Qwen voices. Since 2026-11-08 keeper-of-the-throne
has no effect, because AJ wanted the voice as he heard it. tolstoff-naberius is recurring but never speaks, and the chapter 8 students are
one-off, so the narrator reads them.

## Still to cast

Chapter 12 needs lorimor-ancitif and eole. The cached brief for eole says "around twenty, bright, youthful", so cast
both with these descriptions, planned on 2026-11-07:

* lorimor-ancitif: "A man in his early forties with a smooth, polished, low baritone and a steady chest resonance, a grown man's voice. Ingratiating and courteous, with an even, unhurried scholarly delivery, agreeable on the surface with something cold and calculating underneath."
* eole: "A woman in her late twenties with a cool, clear, low alto voice and a steady chest resonance, a grown woman's voice, with a faint foreign lilt. Proud, cold and contemptuous, speaking slowly and deliberately with regal defiance and no trace of fear."

On 2026-11-07 the design session tried both with Breeze voice design in a throwaway serial, with seeds 12000 to 12002.
eole came out at 161 to 177 Hz and lorimor-ancitif at 97 to 130 Hz, and every candidate matched its sample line. AJ
hasn't heard them, and nothing was filed in the real library.

## Breeze trial of the Keeper and Lauriane

On 2026-11-08 AJ asked whether Breeze voice design can make a whispery Keeper without the effect. The trial ran in
a scratch copy on jsc-m1, `/Users/aj/serial-audio/scratch/keeper`, and nothing was filed in the real library. It
designed four candidates from each of three Keeper briefs and four of Lauriane from her approved brief. Then it
rendered lines 132 to 198 of chapter 2, where Lauriane questions the Keeper, once per Keeper below.

| Scene | Keeper | Voiced share of the Keeper's takes |
|---|---|---|
| 0 | the approved Qwen clip with the whisper effect | 0.14 on the clip |
| 1 | Breeze seed 13100, whisper brief | 0.44 |
| 2 | Breeze seed 13201, half-whisper brief | 0.34 |
| 3 | Breeze seed 13203, half-whisper brief | 0.43 |
| 4 | Breeze seed 13201, with a whisper direction on every Keeper line | 0.17 |

The half-whisper brief of seed 13201: "A low, hollow, rasping half-whisper, airy and breathy with only a faint trace of
voice beneath it, like a spirit speaking softly from an empty stone hall. An ageless servant of no clear sex: flat,
cold and ceremonial, slow and evenly paced, with no emotion at all."

Scene 3 dropped the end of two Keeper lines, and in scene 4 Parakeet heard "Yes." as "Yeah.". Lauriane's four
candidates came out at 150 to 161 Hz, against 184 Hz for her approved Qwen voice, and every one matched its line.
The scenes use her seed 13300.

AJ listened on 2026-11-08 and picked Keeper seed 13000, a voiced clip from the approved brief rather than a whisper,
and Lauriane seed 13302. Both were cast again in the library with the same seeds on jsc-m1, which made the same clips,
and approved.
