# Phase 3: rank takes on delivery

Nothing in this phase retakes a line on its own. The evidence only supports choosing among takes. Read `README.md` in this folder first. Phases 1 and 2 have to be deployed before this phase starts.

Why this phase is weaker than the others (the report, "The wrong word emphasized cannot be checked well enough to fail a take"):

* The best open detector of the stressed word, WhiStress, agreed with listeners on TTS audio at kappa 0.32. The listeners agreed with each other at 0.36.
* On one benchmark, explicit emphasis markup raised the best TTS systems' hit rate only to about 52%.
* Breeze TTS 2 has no emphasis markup, no phoneme input and no scores. Its levers are the text, the direction and the seed.
* Audio-language models answer from the words and not from the voice, so they can't judge a frightened line that was read calmly. Claude doesn't accept audio.

Start each package with a small experiment. Build the package only if the experiment supports it, and write the result in `DECISIONS.md` either way.

## Order

Do P0 first, because it decides what P1 does. P2, P3 and P4 don't depend on each other.

## P0: does Breeze respond

* Take ten lines with a clear focus word. Render each as plain text, with asterisks around the word, with the word in capitals, and as a sentence reworded so its natural stress falls on the word. Use several seeds for each.
* Capitals reach Breeze today. Asterisks never do, because the screenplay pass drops them.
* Score the takes with WhiStress, and give AJ ten pairs to hear.
* Time WhiStress on the laptop's CPU. It is whisper-small.en with a small head, under a non-commercial licence, and nobody has published its speed.
* It is done when `DECISIONS.md` says which text lever, if any, moves the stress, by the detector and by AJ's ear.

## P1: focus ranking

* Phase 1's package C1 records a focus word on lines with a contrast or a correction. Chapters scripted before C1 need the annotation pass that phase 2 adds for heteronyms.
* On a line with a focus word, spend the line's budget on a few seeds even though no rule flagged it. `cpp_take` stops at the first take that fits its length today, so this is a new path. Keep the passing take whose stress the detector places on the focus word.
* If one take hits the right stress 30 to 42% of the time, four independent takes give at least one hit 76 to 89% of the time. Nobody has tested whether Breeze's seeds are independent in this way. Measure it on the ten lines of P0 before you build this.
* It is done when the cost in takes per chapter is known and AJ has heard a chapter's focus lines before and after.

## P2: direction as rank

* Read the class of the direction (C1 splits it into emotion, energy and pace). Lower a take's rank when an emotion or arousal model clearly contradicts it.
* emotion2vec+ large over-calls on TTS audio: it heard "happy" at 0.70 where listeners heard 0.32. So use it against a flat contradiction only.
* This fills the fifth slot of the rank. It never flags a take.

## P3: pitch

* Measure whether a yes-no question rises at its end, whether a take is flat against its character's range, and whether a character's range drifts across takes.
* SwiftF0 tracks 5 s of audio in about 133 ms on a CPU, against about 1.4 s for librosa's pYIN.
* None of this has been validated on TTS, so these are rank inputs.

## P4: quality outliers

* Score each take with a quality predictor against its voice's own median, as a rank input. The predictors rank systems well and single takes poorly. UTMOSv2 reaches 0.92 by system and 0.61 by utterance.
* Measure the room of a cast candidate. AJ heard Casval's new voice as if in a hard-walled room, and no measurement told a roomy clip from a dry one. Brouhaha estimates this, but its model is gated and needs pyannote.

## What this phase should not build

* A judge made of an audio-language model, for the reason above.
* Restoration of every chapter with a generative model. It resynthesizes the voice and can change who it sounds like.
* Bandwidth extension above Breeze's 24 kHz. Road noise and a bedside volume hide most of what it would add.

## What AJ does in this phase

* He approves the PyTorch models. The Mac's audio Python has no torch, so the choice is the laptop only or an install on the Mac.
* He listens to the ten pairs of P0, and to one chapter's focus lines for P1.
