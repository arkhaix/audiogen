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
* Done on 2026-11-13: asterisks around the word are the lever, by the detector and by AJ's ear (`DECISIONS.md`, "Asterisks around a word move the stress onto it"). The 160 takes are ten lines whose chapter text has one word in italics, in the four ways, from four seeds each, made on jsc-m1 in a scratch copy.
* By WhiStress, with the words of the line given, the focus word is stressed in 22 of 40 plain takes, 33 of 40 with asterisks, 37 of 40 with capitals and 11 of 40 reworded. Against the plain take of the same line and seed, asterisks add the stress in 12 takes and lose it in 1, and capitals add it in 15 and lose it in none. The rewordings of this session lost it in 13 and added it in 2, so they say nothing for rewording done well.
* WhiStress runs on the laptop's CPU in 0.62 of the audio's length with the words given and in 1.38 when it has to hear them, on 4 threads. Its two passes give the focus word the same label in 149 of 158 takes. It needs one change for transformers 5.17, which `p0_score.py` makes without touching the clone: the Whisper decoder layer returns a tensor there, and WhiStress reads item 0 of a tuple. `p0_score.py` also checks the audited files by hash, refuses every connection, loads the head's weights as tensors only and hides the card, because WhiStress moves to the card when torch reports one.
* A measure of this session scored the takes too: the length, the level and the highest pitch of the focus word's vowel against the take's other vowels. Nobody has checked it against an ear, and it is weaker than the detector. By it the word's vowel is one of the two most prominent in 15 of 40 plain takes, 17 of 40 with asterisks and 21 of 39 with capitals, and the word is 0.07 s longer with either lever. The takes WhiStress calls stressed have a louder and longer vowel by it.
* Capitals garbled one take in 40, the fourth seed of chapter 33 line 168, where the voice spelled letters out. No take with asterisks went wrong, and nothing shows that the voice says an asterisk.
* For P1's question about seeds, by WhiStress: with the plain text, all four seeds of a line stress the word in 4 lines of 10, at least one does in 8, and none does in 2. With asterisks at least one seed does in all 10 lines and all four in 6, and with capitals all four do in 7.
* AJ got a page of the ten lines on 2026-11-13, each as the plain take, the take with asterisks and the take with capitals from one seed, in an order the page hides: https://claude.ai/artifact/QTnbdg5EWgCSY84CUvP6RU. He heard the word stressed most with asterisks in 5 lines, with capitals in 2 and in the plain take in 1, with no difference in 1 and no stress in 1. `listen\answers1.txt` in the scratch folder has his marks.

## P1: focus ranking

* Phase 1's package C1 records a focus word on lines with a contrast or a correction. Chapters scripted before C1 need the marks pass, `marks <slug> <chapters>`, one Claude pass a chapter. Phase 2 did not add its job, because P5 stopped at its measurement, so this package adds the job kind if it is built.
* On a line with a focus word, spend the line's budget on a few seeds even though no rule flagged it. `cpp_take` stops at the first take that fits its length today, so this is a new path. Keep the passing take whose stress the detector places on the focus word.
* If one take hits the right stress 30 to 42% of the time, four independent takes give at least one hit 76 to 89% of the time. Nobody has tested whether Breeze's seeds are independent in this way. Measure it on the ten lines of P0 before you build this.
* It is done when the cost in takes per chapter is known and AJ has heard a chapter's focus lines before and after.
* Built in another form and deployed on 2026-11-13 at 18:00 (`DECISIONS.md`, "The author's italics reach the voice as asterisks"). P0 showed that a mark around the word does more than a choice among seeds, so the build has no extra seeds and no detector. A line's `emphasis` holds the words its chapter sets in italics for stress, found from `text.md` without a Claude pass, and `voice_text()` puts asterisks around them. `focus` gets none, and the marks pass still has no job.
* The cost is known: 534 of the 11,704 lines of the 53 scripts get the field, 9 in a median chapter, and a chapter's next render makes those lines again.
* For the second half of "done", AJ got a page on 2026-11-13 with 17 of chapter 50's 18 such lines without and with the asterisks, from one seed each, made on jsc-m1: https://claude.ai/artifact/5n8kJYQLCjNNjtAsz2uiT4. He chose that day to let the change ride on the batch he had queued, and marked the page afterwards: the take with asterisks stresses the words more in 12 lines, the plain take in 1, with no difference in 1 and no stress in either in 3. So P1 is done. `listen\answers_p1.txt` has his marks. `p1_takes.py` in `D:\fish.wt\scratch\p3` made the takes, `p1_hear.py` and `p1_page.py` the page, `listen\key_p1.json` says which take of a row is which, and `p1_cost.py` counts what the rule marks.

## P2: direction as rank

* Read the class of the direction (C1 splits it into emotion, energy and pace). Lower a take's rank when an emotion or arousal model clearly contradicts it.
* emotion2vec+ large over-calls on TTS audio: it heard "happy" at 0.70 where listeners heard 0.32. So use it against a flat contradiction only.
* This fills the fifth slot of the rank. It never flags a take.
* Measured on 2026-11-13, and nothing was built (`DECISIONS.md`, "A take's emotion gives the take check nothing to rank on"). AJ allowed the installs that day, after a security audit of the packages found no way for them to send data out or install anything. `p24_sample.py` drew 245 takes of chapters 7 to 48: the 85 whose direction names one emotion (30 happy, 15 angry, 14 disgusted, 12 sad, 8 surprised, 6 fearful) and 160 without a direction. `p2_emotion.py` labelled them with emotion2vec+ large in 355 s on the laptop's CPU, and `p2_report.py` prints the counts. The model gives a strong emotion to 32 of the 160 takes without a direction, picks the directed emotion in 22 of the 85, and flatly contradicts it in 12. So its contradictions are rarer than its false alarms and can't rank a take. It hears almost nothing of anger, fear and disgust, in 1 of the 35 takes directed that way. AJ marked a page of those 35 takes on 2026-11-13, to say whether the voice acts those directions: https://claude.ai/artifact/UhXmMMoCTX5QyQxTZt5LAh. He heard the voice act the direction in 4 takes, partly in 20 and not in 11, and the model gave none of the 4 its direction's emotion. So the voice mostly half-acts these three directions, and the model misses them when it does act them. `p2_page.py` built the page, `listen\key_p2.json` in the scratch folder has each clip's take and the model's label, and `listen\answers_p2.txt` has his marks.

## P3: pitch

* Measure whether a yes-no question rises at its end, whether a take is flat against its character's range, and whether a character's range drifts across takes.
* SwiftF0 tracks 5 s of audio in about 133 ms on a CPU, against about 1.4 s for librosa's pYIN.
* None of this has been validated on TTS, so these are rank inputs.
* Measured on 2026-11-13 over the 4,901 takes of chapters 27 to 48, and nothing was built (`DECISIONS.md`, "A take's pitch gives the take check nothing to rank on yet"). `librosa.pyin` at 16 kHz tracked 8.8 hours in 125 s on the Mac with four processes, so SwiftF0 is not needed. Of 151 yes-no questions 88 rise at the end and 25 fall, against 305 rises in 3,814 statements. A flat take follows its direction, and a voice's level doesn't drift between chapters. The ending of a question is the one measure that could rank a take, after AJ has heard some that fall and after a test of whether another seed changes it. AJ agreed that day to leave pitch out, so P3 is closed and the page of questions that fall and that rise was not made.

## P4: quality outliers

* Score each take with a quality predictor against its voice's own median, as a rank input. The predictors rank systems well and single takes poorly. UTMOSv2 reaches 0.92 by system and 0.61 by utterance.
* Measure the room of a cast candidate. AJ heard Casval's new voice as if in a hard-walled room, and no measurement told a roomy clip from a dry one. Brouhaha estimates this, but its model is gated and needs pyannote.
* Measured on 2026-11-13, and nothing was built (`DECISIONS.md`, "A take's quality score gives the take check nothing to rank on"). `p24_sample.py` drew 294 takes: 30 that the check passed for each of the eight busiest voices, 40 of those a second time for the predictor's own spread, and 14 known ones, of which 9 are bad: the eight labelled takes that came out flagged again and the take of P0 that capitals garbled. `p4_sample2.py` added 19 passed takes of the two voices that have three of the bad takes, because the first draw had none of theirs, and a second score of the 12 lowest takes. `p4_quality.py` scored the 325 with UTMOSv2 in 992 s on the laptop's CPU, 2.9 s a take at the median, and `p4_report.py`, `p4_more.py` and `p4_length.py` print the counts. One of the nine bad takes stands out against its own voice. A voice's median runs from 1.85 to 4.07, a second score of the same take differs by 0.21 at the median where a voice's spread is 0.23, and a take under 1.5 s scores 2 spreads low. The bad takes at hand are bad in their words, so the experiment doesn't show what the score does with a take whose sound is at fault. The room measure is not set up, because Brouhaha needs numpy under 2.0.
* `seconds` in `breeze_takes.json` is the time a take took to make. `p24_sample.py` drew by it, so the takes of P2 and P4 are 0.7 to 27 s of audio and not 2 to 12 s as its text says.

## The models, as a web search found them on 2026-11-13

The laptop's audio Python has torch 2.14, torchaudio 2.11, transformers 5.17 and onnxruntime 1.31. The Mac's venv doesn't have torch, transformers or onnxruntime, and sherpa-onnx there can't run a model of another kind. WhiStress, emotion2vec+ large and UTMOSv2 have run on the laptop's CPU from `D:\fish.wt\scratch\p3` since 2026-11-13, each after a security audit that read its code and weights without running them. Nothing of them is installed on a node or in the audio Python itself: their packages are in `site\` there, and `guard.py` keeps a run off the card and off the network. SwiftF0 and Brouhaha have not run.

* WhiStress, for P0 and P1: the code at https://github.com/slp-rl/WhiStress is MIT, and the weights at `slprl/WhiStress` are CC BY-NC 4.0, "for research purposes only". The head is 42.6 MB in two pickle files, and it sits on `openai/whisper-small.en`, 967 MB as safetensors under Apache 2.0. The laptop needs no new package for it. Its repo pins transformers 4.52.2. It was trained on Google TTS speech only, and its paper gives an F1 of 0.909 on its own synthetic test set, 0.797 on synthetic EmphAssess and 0.689 on real Expresso speech it had not seen.
* emotion2vec+ large, for P2: the weights at `emotion2vec/emotion2vec_plus_large` are one 1.95 GB pickle file under FunASR's model licence, which asks for attribution. It runs through funasr, which brings modelscope, omegaconf, hydra-core, kaldiio, jieba and more. The search found no way to its labels without funasr. Run on 2026-11-13, it needed only funasr 1.4.26, omegaconf, antlr4-python3-runtime and kaldiio beside what the laptop has, so modelscope and the Alibaba packages are not installed. Its files are pinned to revision 6c303ba9 of the repo, because funasr evaluates a string of the repo's `config.yaml`.
* SwiftF0, for P3: `pip install swift-f0`, MIT, with a 135 KB ONNX model inside the wheel. It needs onnxruntime 1.19 or later, which the laptop has and the Mac doesn't. Its README gives 180 times real time on one laptop core. `librosa.pyin` is on both nodes already and is slower.
* UTMOSv2, for P4: MIT, five folds of 819 MB each, of which one is enough. It needs timm and its own package from GitHub. Its maintainer gives 1 to 2 s a sample on a laptop with a card, and nobody gives a CPU speed. Run on 2026-11-13 from the clone at commit cc2700db with timm 1.0.31, it takes about 3 s a take on the laptop's CPU. Its checkpoint is passed by path, because it otherwise starts `wget` or `git` to fetch one. Two predictors need no new package on the laptop: UTMOS22 strong through https://github.com/tarepan/SpeechMOS, one 411 MB pickle file with code that torch hub fetches, and DNSMOS P.835, two ONNX files of 1.4 MB that were built for noise suppressors.
* Brouhaha, for the room of P4: the checkpoint in https://github.com/marianne-m/brouhaha-vad is 47 MB and not gated, where the copy on Hugging Face is. It needs pyannote.audio under 3.3.1, speechbrain under 1.0 and numpy under 2.0, and both nodes have numpy 2.5. The search found no ONNX model that estimates a room.

## What this phase should not build

* A judge made of an audio-language model, for the reason above.
* Restoration of every chapter with a generative model. It resynthesizes the voice and can change who it sounds like.
* Bandwidth extension above Breeze's 24 kHz. Road noise and a bedside volume hide most of what it would add.

## What AJ does in this phase

* He approves the PyTorch models. The Mac's audio Python has no torch, so the choice is the laptop only or an install on the Mac.
* He listens to the ten pairs of P0, and to one chapter's focus lines for P1.
