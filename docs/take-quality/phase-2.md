# Phase 2: three small CPU models and a second recognizer

This phase changes hosts, so it starts with a list for AJ to approve. Read `README.md` in this folder first. Phase 1 has to be deployed before this phase starts, because every package here reads its evidence record.

Checked on the laptop on 2026-11-12: the installed sherpa-onnx 2.0.0 has `VoiceActivityDetector`, `SpeakerEmbeddingExtractor` and a loader for NeMo CTC models. So the speech detector and the speaker embedding should need model files only, not new packages. Nobody has checked the Mac.

## Order

Do P0 first and wait for AJ. Then P1, P2 and P3 can run side by side. P4 comes after AJ has made picks on the Names page. P5 comes last.

## P0: the spike and the host list

* Try each model on both CPUs and time it on a few saved takes. None of them has a published speed on this hardware.
* Choose a speech detector. Silero VAD and TEN VAD both run through sherpa-onnx.
* Choose a speaker embedding. sherpa-onnx has CAM++, ERes2NetV2, WeSpeaker and TitaNet models of about 26 to 221 MB. Pick the one that best tells AJ's cast apart and is steady within one voice.
* Choose a second recognizer from another family than Parakeet that runs on CPU through sherpa-onnx. Don't use Qwen3-ASR for this, because it needs the laptop's card and the Mac doesn't have ComfyUI. A Whisper model wrote text for 40% of non-speech clips in one study (the report, "Build in three phases"), so test it on the shortest takes before you choose it.
* Choose a phoneme recognizer. Try ZIPA small through sherpa-onnx's Zipformer CTC loader. ZIPA's own ONNX code imports torch and lhotse, which the Mac's venv doesn't have. If the loader refuses the file, the fallback is wav2vec2-lv-60-espeak under torch, on the laptop only.
* Add a variable in `machine.py` for each model's path, and a file check for each in `asr.check_ready`.
* Give AJ the list: each file with its source, size and licence, the folder on each node, and the lines for each node's `serial_audio_config`. The Nomad client configs live in `~/projects/ansible`. Don't download a model to a node or change a config before he says yes.
* It is done when AJ has approved the list, the files are on both nodes, and `docs/machines.md` says so.

## P1: tails

* Use the speech detector to find sound after the last word of a take: a breath, a hum or babble. Phase 1 left this alone on purpose.
* Trim it when the recognizer heard no words in it. Retake when it did.
* It is done when `measure-takes` shows how many takes have a tail, the lead has read a sample, and a scratch render trims one.

## P2: voice drift

* Breeze users report a different timbre across segments with the same reference and seed.
* Compare an embedding of each take with the centre of its voice's accepted takes, and not with the cast clip. Retake an outlier, under the voice breaker.
* The embeddings don't see accent. The French accent AJ kept hearing on Argas Septic is out of reach of this check.
* It is done when `measure-takes` shows each voice's spread, the threshold is set per voice, and the centres are stored where a render on either node can read them.

## P3: the second opinion

* Let the second recognizer hear a flagged or a suspect take too. Flag a suspect take that both recognizers hear wrong outside names. This makes the suspect band act for the first time.
* A flagged line's rounds already produce up to two more takes. Choose the one to keep by rank across both recognizers, in place of the first take that is closer. A verifier from the family that judges overstates its own gain two to three times (the report, "The field still checks takes"), so the two families have to differ.
* It is done when the suspect takes of The Hundred Reigns are split into those both recognizers hear wrong and the rest, and the lead has read a sample of each.

## P4: names held to the pick

* Score the span of a name in a take against the candidate AJ picked. The token times of phase 1 give the span. Retake an outlier.
* Decode phonemes freely from a CTC model, with no text prompt. A recognizer that is given the text hears what it expects.
* Use the same recognizer to screen D2's candidates over several seeds, so a respelling that comes out several ways is never offered. A pick means little if the voice doesn't repeat it.
* Replace D1's counts of spellings with what was said.
* No accuracy figure exists for any of this on synthetic voices. AJ's own picks are the labelled cases. Louis as "Lewis" and Lauriane as "Laurie Anne" are the first two.
* It is done when a take that says a picked name another way is retaken in a scratch render, and the takes that say it right are left alone.
* Built on 2026-11-13 as a rule on added sounds, which hears part of this. `p4-name-check.md` has what it misses and the steps from here.

## P5: heteronyms

* Phase 1's package C1 records the sense of a heteronym in each new script. Chapters scripted before C1 don't have it. Give them a small pass that annotates the lines and leaves the text alone, so no line number moves.
* Respell the rarer sense for that line only, in `voice_text()`. This changes the key of those lines, so count them before the deploy.
* Check the take between the two known readings with the phoneme recognizer.
* Nobody has measured how often Breeze misreads a heteronym. Measure that first, and stop here if it is rare.
* Measured on 2026-11-13, and it is rare, so P5 stopped there and nothing was built (`DECISIONS.md`, "A heteronym gets no respelling and no check"). Of 253 sayings in The Hundred Reigns that the audio could judge, 249 have the reading their line means and 4 are doubtful. The measurement also found that ZIPA writes one reading of a spelling whatever the take says, so the check of the third point can't use its phones. The pass of the first point was not added. The package of phase 3 that first reads a mark adds its job.

## The end of the phase

* Ask AJ which chapter he is on, and run the backlog pass.

## What AJ does in this phase

* He approves the host list of P0.
* He makes the changes to the Nomad client configs, or approves them.
* He makes picks on the Names page before P4.
