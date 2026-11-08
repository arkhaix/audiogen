# Casting

`README.md`, step 3 of the pipeline, describes how `cast` works. There are two engines, chosen by `cast_engine` in
`serial.yaml` or `cast --engine`:

* `qwen`, the default: Qwen3-TTS VoiceDesign in ComfyUI designs the candidates, and each is also encoded as a Drama 3 voice.
* `breeze`: breeze-server designs each candidate from the brief with no reference clip. There is no Drama 3 voice file, so the voice can't be used for a Drama 3 render.

Each candidate is transcribed and compared with its sample line, and the best match is filed with status `auto`
until AJ listens and runs `pick-voice`.

---

## Writing a brief

* Name an age older than the character's age in the story: late twenties for a 20-year-old, mid fifties for a late-forties empress. Name a low register and chest resonance. Together these dropped pitch 20 to 40 percent in the 2026-11-05 recast, after AJ found the first cast too young.
* The casting pass still writes young characters as early or mid twenties. Override its brief with `--instruct` when it does.
* "Theatrical" and "dramatic swells" push the pitch up, even next to "baritone". So do "bright" and "easy laugh".
* "Genderless" comes out as a young woman, and VoiceDesign can't make an inhuman voice. The Keeper of the Throne uses a post-render effect instead: `voice_effects` in `serial.yaml`, a whisper at voice level 0.35.
* AJ turned down "weathered, gravelly" for a church leader and wanted a performative voice.

## Screening before AJ listens

* The median F0 of each candidate clip, from librosa.pyin with fmin 60 and fmax 500 under the audio Python, is a quick check on pitch and on how far apart two voices sit.
* Use a new seed range for each round, so `pick-voice --seed` names exactly one candidate. Used so far: 1000, 4000, 5000, 6000, 7000, 9000, 10000, 11000, and 12000 in a throwaway serial. The next round should start at 13000.

---

## The Hundred Reigns cast

Median F0 of the filed clips, measured from 2026-11-05 to 2026-11-07, and each voice's status in the library on
containers on 2026-11-08:

| Voice | F0 | Status |
|---|---|---|
| narrator | 81 Hz | approved |
| simon | 105 Hz | approved |
| thalas | 108 Hz | approved |
| euphemia | 132 Hz | approved |
| louis | 149 Hz | approved |
| mastemo | 155 Hz | approved |
| lauriane | 184 Hz | approved |
| agnes-firewand | 208 Hz | approved |
| keeper-of-the-throne | unknown | approved |
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

A voice with status `auto` was filed by the casting check, and AJ hasn't approved it. keeper-of-the-throne is
read with the whisper effect. tolstoff-naberius is recurring but never speaks, and the chapter 8 students are
one-off, so the narrator reads them.

## Still to cast

Chapter 12 needs lorimor-ancitif and eole. The cached brief for eole says "around twenty, bright, youthful", so cast
both with these descriptions, planned on 2026-11-07:

* lorimor-ancitif: "A man in his early forties with a smooth, polished, low baritone and a steady chest resonance, a grown man's voice. Ingratiating and courteous, with an even, unhurried scholarly delivery, agreeable on the surface with something cold and calculating underneath."
* eole: "A woman in her late twenties with a cool, clear, low alto voice and a steady chest resonance, a grown woman's voice, with a faint foreign lilt. Proud, cold and contemptuous, speaking slowly and deliberately with regal defiance and no trace of fear."

On 2026-11-07 the design session tried both with Breeze voice design in a throwaway serial, with seeds 12000 to 12002.
eole came out at 161 to 177 Hz and lorimor-ancitif at 97 to 130 Hz, and every candidate matched its sample line. AJ
hasn't heard them, and nothing was filed in the real library.
