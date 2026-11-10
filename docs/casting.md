# Casting

`README.md`, step 3 of the pipeline, describes how `cast` works. There are two engines, chosen by `cast_engine` in
`serial.yaml` or `cast --engine`:

* `breeze`, the default since 2026-11-08: breeze-server designs each candidate from the brief with no reference clip. There is no Drama 3 voice file, so the voice can't be used for a Drama 3 render.
* `qwen`: Qwen3-TTS VoiceDesign in ComfyUI designs the candidates, and each is also encoded as a Drama 3 voice.

Each candidate is transcribed and compared with its sample line, and the best match is filed with status `auto`
until AJ listens and runs `pick-voice`.

---

## Writing a brief

* For a Breeze cast, name the character's age in the story, and leave out "low register", "chest resonance" and "a grown man's voice". AJ decided this on 2026-11-09, after the aged-up Qwen voices of Casval and Anna sounded too old to him and he picked Breeze candidates from "about twenty" briefs. With Breeze, the "about twenty" brief and the aged-up brief gave the same pitch range.
* For a Qwen cast only, name an age older than the character's age in the story: late twenties for a 20-year-old, mid fifties for a late-forties empress. Name a low register and chest resonance. Together these dropped pitch 20 to 40 percent in the 2026-11-05 Qwen recast, after AJ found the first cast too young.
* A cast with no voice description uses the brief the casting pass cached, which follows the story's age. That suits a Breeze cast. A brief cached before 2026-11-09 may have been overridden with an aged-up description, as Casval's and Anna's were, so read the cached brief with a dry run before a recast.
* For Breeze, write "clear" and "speaking at a normal conversational volume", and don't describe the character as "quiet". "Quiet and watchful" gave Casval eight candidates that all sounded as if he stood back from the mic in a hard room, and "recorded close to the microphone in a quiet, acoustically dead studio" did not fix it. The volume wording gave seven clear candidates out of eight AJ heard (the Casval section below).
* A clone takes on the room of its cast clip, so let AJ judge a few chapter lines cloned from each candidate, not the sample clip alone. AJ picked Casval's seed 18001 from its clip and heard the reverb only in the chapter.
* "Theatrical" and "dramatic swells" push the pitch up, even next to "baritone". So do "bright" and "easy laugh".
* "Genderless" comes out as a young woman, and Qwen VoiceDesign can't make an inhuman voice. The Qwen Keeper of the Throne used a post-render whisper effect instead, at voice level 0.35. `voice-effect <slug> <voice> whisper` sets one in `voice_effects` in `serial.yaml`, and `--clear` removes it.
* Breeze voice design whispers only when the brief makes the whisper the voice itself. On 2026-11-08 "speaks only in a hushed, breathy whisper, with no voiced tone at all" and "a rasping half-whisper" each gave one true whisper in four candidates. The Keeper's approved brief, which names "a dry, rasping whisper" among other things, gave none.
* A Breeze clone of a whispered clip drifts back toward a full voice on about half its lines. A direction of "a hushed, breathy whisper" on every line holds the whisper. Breeze rendering has no per-voice direction, so the 2026-11-08 trial wrote it into a scratch script.
* AJ turned down "weathered, gravelly" for a church leader and wanted a performative voice.

## Screening before AJ listens

* The median F0 of each candidate clip, from librosa.pyin with fmin 60 and fmax 500 under the audio Python, is a quick check on pitch and on how far apart two voices sit.
* The share of speech frames that pyin calls voiced tells a whisper from a voice. Normal speech scores about 0.65, the Keeper's Qwen clip with the whisper effect 0.14, and a true Breeze whisper 0.11 to 0.13.
* A Breeze take comes out at the level of its voice's cast clip, and cast clips run from -11 to -29 dB of active speech RMS. Since 2026-11-09 the join levels each voice, so a chapter doesn't show the difference, but raw candidate clips do. Bring candidates to one level before AJ compares them by ear, or a quiet clip sounds worse than it is.
* The casting check files the candidate whose transcript matches best. For a whisper brief that is the most voiced candidate, so pick a whispered one by seed.
* Use a new seed range for each round, so `pick-voice --seed` names exactly one candidate. Used so far: 1000, 4000, 5000, 6000, 7000, 9000, 10000, 11000, 12000 in a throwaway serial, and 13000, 13100, 13200 and 13300 in a scratch copy, 17000 and 18000 in the library (eole and lorimor-ancitif), and 18000, 18100, 18200 and 18300 in a scratch copy (casval and anna-paimon), 19000, 19100 and 19200 in a scratch copy (lorimor-ancitif), and 20000, 21000, 21100, 21200, 22000, 22100, 22200 and 22300 in a scratch copy (casval). The next round should start at 23000.

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
| casval | 142 Hz | approved |
| unnamed-minister | 129 Hz | auto |
| cassandra | 142 Hz | auto |
| shabram-gremory | 157 Hz | auto |
| antonine-de-shax | 172 Hz | auto |
| meredith-dose | 189 Hz | auto |
| tiella-decarabia | 190 Hz | auto |
| anna-paimon | 180 Hz | approved |
| gourmand | unknown | auto |
| unnamed-branded-voice | unknown | auto |

A voice with status `auto` was filed by the casting check, and AJ hasn't approved it. keeper-of-the-throne and
lauriane are Breeze voices since 2026-11-08, casval and anna-paimon since 2026-11-09 (their rows are from that day),
and the rest are Qwen voices. Since 2026-11-08 keeper-of-the-throne
has no effect, because AJ wanted the voice as he heard it. tolstoff-naberius is recurring but never speaks, and the chapter 8 students are
one-off, so the narrator reads them.

## Cast, not yet picked: lorimor-ancitif and eole

Both were cast on 2026-11-08 with Breeze from the cached briefs, seeds 17000 to 17002, and seed 17000 was filed for
each with status auto. The cached brief for eole says "around twenty, bright, youthful" and lorimor-ancitif's says
"late thirties, mid-pitched", so this cast did not use the older, lower descriptions planned on 2026-11-07, below. AJ
hasn't listened. If they sound too young, recast each one from the Cast tab with its planned description in the
voice description field (a description applies to one voice at a time):

* lorimor-ancitif: "A man in his early forties with a smooth, polished, low baritone and a steady chest resonance, a grown man's voice. Ingratiating and courteous, with an even, unhurried scholarly delivery, agreeable on the surface with something cold and calculating underneath."
* eole: "A woman in her late twenties with a cool, clear, low alto voice and a steady chest resonance, a grown woman's voice, with a faint foreign lilt. Proud, cold and contemptuous, speaking slowly and deliberately with regal defiance and no trace of fear."

On 2026-11-07 the design session tried both with Breeze voice design in a throwaway serial, with seeds 12000 to 12002.
eole came out at 161 to 177 Hz and lorimor-ancitif at 97 to 130 Hz, and every candidate matched its sample line. AJ
hasn't heard them, and nothing was filed in the real library.

## Casval's room sound

On 2026-11-09 AJ heard echo or reverb on Casval's new voice, seed 18001, in chapter 11: "like he's in a room with
hard walls and has stepped back from the mic". A scratch round cloned five chapter 11 lines from seeds 18000 to
18003 and from four candidates of the same brief plus "recorded close to the microphone in a quiet, acoustically
dead studio" wording, seeds 20000 to 20003. AJ heard the reverb in all eight, to varying degrees, so the studio
wording did not remove it. A second round, seeds 21000, 21100 and 21200, dropped "level, cool" and "quiet and
watchful" from the brief. Of the five AJ heard from it, only seed 21100 sounded clear, from a brief with "firm,
clear" and "speaking at a normal conversational volume", but its performance did not match the character. A third
round, seeds 22000 to 22300 with six candidates each, keeps "clear" and the volume wording and puts the calm,
deadpan character back. AJ heard six of them, seeds 22001, 22104, 22200, 22201, 22205 and 22300, found all six
clear with no reverb, and picked seed 22200, at 142 Hz, which was recast in the library and approved. The filed clip
differs from the scratch clip only by rounding (correlation 0.99998). Chapters 11 and 17 were rendered again with
it. Chapters 8 and 10 keep his Qwen voice, and chapters 7, 8 and 10 keep Anna's, because AJ had finished them. Its brief: "A young man of about twenty with a clear, steady baritone, speaking at a normal conversational
volume. Dry, understated and deadpan, with a calm, even, formal delivery and almost no inflection." No measurement
here told a roomy clip from a dry one: a decay time followed the speaker, and a modulation ratio followed the pitch
almost exactly.

What AJ's ears say about the cause, over 19 candidates: all seven clear ones had "speaking at a normal
conversational volume" in the brief and a median F0 of 132 Hz or more. All eight from the "quiet and watchful"
brief were roomy, at 97 to 118 Hz, with or without the studio wording. Of the four roomy ones in round two, seed
21101 had the volume wording at 116 Hz, and seed 21202 lacked it at 138 Hz. So the volume wording helps, a low
pitch may hurt, and the two can't be told apart from this.

## Lorimor recast with his madness

lorimor-ancitif's first brief was cached at chapter 12, before the bible recorded his fanaticism, and its "late
thirties" was the casting pass's guess. He was recast with Breeze seeds 18000 to 18003 on 2026-11-08 and seed 18001
was approved. On 2026-11-09 AJ read the bio, heard no fanaticism or madness in the voice, and asked for a recast. A
scratch trial on the laptop designed four candidates from each of three briefs, seeds 19000, 19100 and 19200, and
AJ picked seed 19000. It was recast in the library with that seed and approved, and the filed clip is the scratch
clip byte for byte. Its brief gives no age: "A man with an educated, mid-pitched voice with a slightly cracked edge.
A courteous, fawning scholar, soft-spoken and wistful, whose politeness trembles with barely contained religious
fervor, like a true believer who is quietly, unshakably mad." "Trembles" and "cracked" pushed the pitch up: the
candidates ran from 131 to 211 Hz against 136 Hz for the old clip, and seed 19000 is 147 Hz. As of 2026-11-09 his
chapters, 12, 13, 15, 16 and 17, still have the old voice.

## Breeze trial of Casval and Anna at their story age

On 2026-11-09 AJ said Casval and Anna sound too old for the academy arc, where they should sound about 20, and
questioned the older-age rule now that casting uses Breeze. Both were Qwen voices from aged-up briefs, "early
thirties" and "late twenties". The trial ran in a scratch copy on the laptop. It designed four Breeze candidates from an "about twenty" brief with no "low", "chest resonance" or "grown"
wording, and four from each current brief as a control.

| Voice | Brief | Seeds | Median F0 |
|---|---|---|---|
| casval | the filed Qwen clip | 10000 | 121 Hz |
| casval | about twenty | 18000 to 18003 | 103 to 117 Hz |
| casval | early thirties, the current brief | 18200 to 18203 | 95 to 116 Hz |
| anna-paimon | the filed Qwen clip | 10000 | 239 Hz |
| anna-paimon | about twenty | 18100 to 18103 | 180 to 205 Hz |
| anna-paimon | late twenties, the current brief | 18300 to 18303 | 180 to 193 Hz |

Every candidate matched its sample line. With Breeze the brief's age barely moved the pitch, and neither brief came
out high, so median F0 does not show the age AJ hears. AJ listened the same day and picked Casval seed 18001 and
Anna seed 18103, both from the "about twenty" briefs. Each was recast in the library on the laptop with its brief, its
seed and one candidate, and approved. Anna's filed clip is the scratch clip byte for byte, and Casval's differs from
it only by rounding (correlation 0.99997). Chapters 11 and 17 were rendered again with their last seeds, 2 and 3, so
only the two voices' lines were retaken. Chapters 7, 8 and 10 keep the Qwen voices, because AJ had finished them.
The "about twenty" briefs:

* casval: "A young man of about twenty with a level, cool, mid-to-low voice. Dry, clipped, deadpan delivery in short formal phrases with almost no inflection, quiet and watchful."
* anna-paimon: "A young woman of about twenty with a warm, bright, mid-pitched voice. Playful and teasing, quick, confident and chatty, a casual aristocrat with an easy laugh in her voice, who can turn icy and commanding in an instant."

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
