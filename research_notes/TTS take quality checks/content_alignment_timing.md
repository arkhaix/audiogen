# Content fidelity and timing checks for single TTS takes (state as of November 2026)

Scope: checks that a take says its script line (hallucination, looping, skipped words, early cutoff) and that its timing is right (pauses, drawn-out or rushed words, trailing audio). Pronunciation scoring, prosody, loudness and audio artifacts belong to other researchers and are left out. Dates are release or arXiv dates. "Inference" lines are my reasoning about the audiogen pipeline, not claims from a source.

## 1. Beyond one recognizer's WER: how papers and pipelines detect hallucination, looping, skipped words and early cutoff

### Takeaway
Published TTS benchmarks (Seed-TTS-eval, F5-TTS, CV3-Eval) only report corpus-level WER from one recognizer. Per-take pass or fail rules come from 2024 to 2026 robustness and data papers. Those rules are exact match with a small edit allowance, contiguous-run definitions of skips and hallucinations, agreement between two recognizers from different model families, Whisper's decode-failure thresholds (gzip ratio 2.4, mean log-prob -1.0, no-speech 0.6), and forced-alignment posteriors. A forced-alignment detector reached about 95% recall but only about 49% precision, because it over-flags rare words, so it should trigger a retake and never a hard rejection.

### Cited Findings

Benchmark protocols (corpus level, no per-utterance threshold)
- Seed-TTS-eval (ByteDance) scores English with Whisper-large-v3 WER, Mandarin with Paraformer-zh, and speaker similarity (SIM) with WavLM-large fine-tuned for speaker verification. It runs as `cal_wer.sh` and `cal_sim.sh` over a whole test set. ([BytedanceSpeech/seed-tts-eval](https://github.com/BytedanceSpeech/seed-tts-eval))
- Copies of F5-TTS's Seed-TTS eval script load faster-whisper-large-v3 for English rather than the original whisper-large-v3, so their WER may differ slightly from the official script. This comes from a search-result summary, and I did not open the file. ([F5-TTS eval_seedtts_testset.py, HF copy](https://huggingface.co/spaces/ivanusto/E2-F5-TTS/blob/main/src/f5_tts/eval/eval_seedtts_testset.py))
- CosyVoice 3 (arXiv 2505.17589, May 2025) ships CV3-Eval with Whisper-large-v3 for English and Paraformer for Chinese. Its DiffRO post-training (a reward model applied after pre-training) lowered hard-English WER from 9.04% to 7.60% (0.5B) and from 10.55% to 7.56% (1.5B), while SIM dipped from 75.9 to 73.9 for the 0.5B model. ([CosyVoice 3](https://arxiv.org/html/2505.17589v2))
- Emilia-Pipe (the Amphion data pipeline behind Emilia) keeps 3 to 30 s segments that pass language-ID confidence of at least 80%, a DNSMOS P.835 OVRL above 3.0, and an interquartile-range outlier test on character and phone durations. ([Emilia, arXiv 2407.05361](https://arxiv.org/html/2407.05361v3)) The two paper versions report different retention: 29.43% (176.22 h) in the 2024 version and 38.75% (258.44 h) in the 2025 version. ([Emilia, arXiv 2501.15907](https://arxiv.org/pdf/2501.15907))

Per-take failure definitions used in 2024 to 2026 papers
- "Taming Long-form Text-to-Speech" (Wang et al., arXiv 2609.16989, Sept 2026) defines "A skip is a contiguous deletion of 10 or more words" and "a hallucination is a contiguous mix of substitutions and insertions of 20 or more words". It measures WER with Parakeet-TDT-0.6B-v2 and Whisper-style text normalization. Its detector reads the TTS model's own alignment attention heads, not an ASR. On recovery it rolls back to the failure onset, reseeds the RNG and applies a hard attention mask, with a capped number of retries (cap not stated). ([arXiv 2609.16989](https://arxiv.org/html/2609.16989v1))
- In that paper, Qwen3-TTS-0.6B baseline WER (mean / worst over 10 seeds) rises from 2.7 / 5.4 for prompts under 500 words to 16.6 / 35.2 at 1,500 words or more. ([arXiv 2609.16989](https://arxiv.org/html/2609.16989v1))
- AlignDPO (Zhou et al., arXiv 2609.12855, Sept 2026) computes a content hallucination rate "from a forced-alignment detector over Parakeet-CTC-1.1B emissions" that flags skipped, repeated or fabricated words. On 160 human-validated utterances it has about 95% recall and 49% precision (inter-annotator kappa 0.78). The authors say it "over-flags low-confidence rare words". ([AlignDPO](https://arxiv.org/html/2609.12855v1))
- AlignDPO's severe metric (SEV-HAL) counts utterances with at least four hallucinated content words, with function words removed by a fixed closed-class list. ([AlignDPO](https://arxiv.org/html/2609.12855v1))
- AlignDPO splits candidates into hallucination-free and hallucinated sets "by exact word match, with one edit tolerated for texts under ten words and any deletion forcing" the hallucinated set. It keeps a preference pair only if both candidates agree across two independent ASR systems. ([AlignDPO](https://arxiv.org/html/2609.12855v1))
- Koel-TTS (NVIDIA, arXiv 2502.05236, Feb 2025) ranks candidates by CER from Parakeet TDT 1.1B and speaker similarity from TitaNet-large (Pareto ranking, lower CER first). On 91 hard sentences with repeated words, CER/WER stayed high: 5.62 / 10.60 at baseline and 5.03 / 9.19 after preference alignment plus CFG for the multi-encoder model. ([Koel-TTS](https://arxiv.org/html/2502.05236v1))
- T5-TTS (Neekhara et al., arXiv 2406.17957, June 2024) ties hallucinations to attention errors that cause missing or repeated words. A monotonic alignment prior plus a CTC loss cut CER on challenging texts from 9.03% to 3.92%. ([arXiv 2406.17957](https://arxiv.org/pdf/2406.17957))

Multi-recognizer agreement
- TouchTTS (arXiv 2412.08237, Dec 2024) uses "Whisper and Paraformer as ASR and Copilot-ASR models" and filters "data with WER greater than 10 and PER greater than 5" between the two transcriptions (units not stated). Its rationale: "a single ASR model may make errors, but the probability of two ASR models making errors is significantly lower." It kept 650k of 1,260k hours (51.6%), and audiobooks kept more than outdoor broadcasts. ([TouchTTS](https://arxiv.org/html/2412.08237))
- "Best-of-N TTS Evaluation is Confounded by ASR Family Alignment" (Yu and Kang, KAIST, arXiv 2607.08256, Aug 2026) finds that a verifier recognizer looks best when the judging recognizer comes from the same family. For example, a same-family verifier recovered 26.0% of oracle headroom against 7.9% for a cross-family one under a Whisper evaluator. The paper recommends reporting under "at least two ASR families with disjoint training lineages". ([arXiv 2607.08256](https://arxiv.org/html/2607.08256))

Recognizer confidence, log-probabilities, compression ratio and no-speech probability
- openai-whisper `transcribe()` defaults: `compression_ratio_threshold=2.4` ("If the gzip compression ratio is above this value, treat as failed", commented "too repetitive"), `logprob_threshold=-1.0` (average log-probability over sampled tokens), `no_speech_threshold=0.6` (a segment counts as silent only when no-speech probability is above 0.6 AND average log-probability is below -1.0), and a temperature fallback of (0.0, 0.2, 0.4, 0.6, 0.8, 1.0) on failure. `hallucination_silence_threshold` (default None) skips silent periods longer than a threshold "when a possible hallucination is detected", and it needs `word_timestamps`. ([whisper/transcribe.py](https://github.com/openai/whisper/blob/main/whisper/transcribe.py))
- On non-speech audio (AudioSet, MUSAN, UrbanSound8K, FSD50K), Whisper large-v3 hallucinated text for 40.3% of inputs. The authors treat looping as its own hallucination type and build a "Bag of Hallucinations" of common phantom outputs for filtering (Barański et al., arXiv 2501.11378, Jan 2025, accepted to ICASSP 2025). ([arXiv 2501.11378](https://arxiv.org/abs/2501.11378))
- sherpa-onnx's C++ `OfflineRecognizerResult` documents `text`, `tokens`, `timestamps` ("Per-token timestamps in seconds when available"), `durations` ("Non-empty only for TDT-style models"), `json`, `lang`, `emotion` and `event`. It documents no log-probability field. ([sherpa-onnx C++ API](https://k2-fsa.github.io/sherpa/onnx/c-api/html/structsherpa__onnx_1_1cxx_1_1OfflineRecognizerResult.html))
- sherpa-onnx PR #2897 tried to add full-vocabulary log-probabilities "to use with advanced confidence scoring". It names a `token_log_probs` JSON field that "Maintains compatibility with upstream's `ys_log_probs` field". It lists offline Whisper, Moonshine, Canary and CTC decoders, not NeMo TDT. The PR shows as closed, with the last visible review on 7 April 2026. ([k2-fsa/sherpa-onnx#2897](https://github.com/k2-fsa/sherpa-onnx/pull/2897))

Alignment scores as hallucination signals
- AlignDPO's alignment signal comes from a wav2vec2-CTC forced aligner: "a mean word-level posterior and a 'stretch ratio'". ([AlignDPO](https://arxiv.org/html/2609.12855v1))
- An "Optimal Alignment Score" (Viterbi over attention) was proposed for CosyVoice2 against repeated or omitted speech (arXiv 2509.19852, Sept 2025). The preprint was withdrawn in Feb 2026, and its abstract page gives no thresholds. ([arXiv 2509.19852](https://arxiv.org/abs/2509.19852))

Tool records for content checks

| Tool | Defect it detects | Published accuracy or threshold | Size | CPU speed | Format | Licence | Released | Fix it enables (inference) |
|---|---|---|---|---|---|---|---|---|
| Parakeet TDT 0.6B v2 (in use) | inserted, dropped, substituted words | LibriSpeech clean 1.69%, other 3.19%, Open ASR average 6.05% ([card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2)) | 600M params, sherpa-onnx int8 encoder 622 MB ([sherpa models](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-transducer/nemo-transducer-models.html)) | RTF 0.118 on 2 threads (machine not named), 0.088 on 4 Cortex-A76 cores ([sherpa models](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-transducer/nemo-transducer-models.html)) | ONNX via sherpa-onnx, NeMo | CC-BY-4.0 ([card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2)) | card dated 05/01/2025 ([card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2)) | retake, pick best of N |
| Qwen3-ASR 0.6B and 1.7B | second opinion from another family | LibriSpeech clean/other 2.11/4.55 (0.6B), 1.63/3.38 (1.7B) ([report](https://arxiv.org/html/2601.21337)) | 0.6B, 1.7B | not found | qwen-asr package (transformers, vLLM) ([card](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B)) | Apache 2.0 ([report](https://arxiv.org/html/2601.21337)) | report Jan 2026 ([report](https://arxiv.org/html/2601.21337)) | cross-family agreement before a retake |
| Parakeet-CTC-1.1B forced-alignment detector | skipped, repeated, fabricated words | about 95% recall, 49% precision ([AlignDPO](https://arxiv.org/html/2609.12855v1)) | 1.1B | not found | NeMo | not checked | paper Sept 2026 | retake, with tolerance for rare words |
| Whisper decode signals | loops (gzip ratio), low confidence, silence | 2.4, -1.0, 0.6 ([transcribe.py](https://github.com/openai/whisper/blob/main/whisper/transcribe.py)) | model dependent | not checked | PyTorch, whisper.cpp, faster-whisper | not checked | defaults long standing | retake |
| Two-recognizer ROVER (TouchTTS) | either recognizer's errors | drop when WER > 10 and PER > 5 between transcripts ([TouchTTS](https://arxiv.org/html/2412.08237)) | sum of two models | sum of two models | any | n/a | Dec 2024 | flag only where both disagree with the script |

### Inferences
- audiogen's rule (three or more inserted or dropped words) sits between AlignDPO's strict rule (one edit allowed under ten words, any deletion fails) and the long-form definitions (10-word skip, 20-word hallucination). For 220-line chapters of short lines, the AlignDPO style is the closer published model. It suggests counting any deletion of a content word as a failure, at least for words that are not invented names.
- The "suspect" band (WER over 0.34 driven by invented names) could become actionable with a character-level or phoneme-level comparison on just the mismatched words. TouchTTS uses a phoneme error rate next to WER for this reason. A name the recognizer spells its own way gives a word error but a small character or phoneme distance, while a skipped or garbled name gives a large one. A phoneme comparison needs a G2P tool, which the audio Python does not have today.
- Qwen3-ASR is already a configured recognizer (`SERIAL_AUDIO_ASR=qwen3`) and comes from a different family than Parakeet. Running it only on takes that Parakeet flags or marks suspect follows the TouchTTS and AlignDPO agreement idea at small cost: retake when both disagree with the script, accept when Qwen3 matches.
- Whisper-family recognizers are a poor choice for the trailing-audio and short-take cases, given the 40.3% non-speech hallucination rate. Whisper's gzip-ratio test is designed for 30 s windows, so a short line rarely reaches 2.4. A transcript-to-script length ratio (inserted words) is the more direct loop signal for one line.
- Early cutoff shows up as deletions at the end of the line in the Levenshtein alignment. Weighting a deletion of the last one or two script words as a failure on its own (whatever the total count) would catch it, since the length guard only sees total duration.

### Gaps
- I found no per-utterance pass or fail threshold inside Seed-TTS-eval, F5-TTS's eval or CV3-Eval. They appear to report corpus-level metrics only, but I did not read every script.
- Whether the current sherpa-onnx Python API exposes token log-probabilities for an offline NeMo TDT model is unconfirmed. The documented C++ struct has no such field, and PR #2897 did not merge.
- The AlignDPO HTML gives no numeric alignment-score thresholds for a skipped or repeated word.
- I found no published precision or recall for word-level WER rules on short TTS lines.

## 2. Forced aligners with word and phoneme timestamps in 2026

### Takeaway
On human speech, MFA 3.x is still the most accurate word-boundary aligner (about 20 ms mean error). The CTC aligners land at 43 to 50 ms (MMS), NeMo NFA at 78 to 89 ms, and WhisperX at about 110 ms. Qwen3-ForcedAligner-0.6B (Jan 2026, Apache 2.0) claims 32 to 43 ms against MFA-style and human labels, but it was trained on MFA pseudo-labels. Parakeet TDT, which audiogen already runs, returns per-token timestamps on an 80 ms grid and TDT durations through sherpa-onnx at no extra cost. That precision is enough for pause and trailing-audio checks, but no published accuracy figure exists for it.

### Cited Findings
- McAuliffe et al., "Montreal Forced Aligner and the state of speech-to-text alignment in 2026" (arXiv 2606.18466, 16 June 2026), gives mean word-boundary error in ms on TIMIT / Buckeye: MFA ARPA 3.0 19.93 / 21.75, MFA Global 3.0 22.33 / 25.35, MMS 43.06 / 49.54, NeMo 78.24 / 88.62, WhisperX 110.04 / 110.90. Within 50 ms: MFA ARPA 3.0 91.61% / 91.35%, MMS 63.73% / 61.85%, NeMo 38.23% / 35.81%, WhisperX 15.55% / 13.48%. ([arXiv 2606.18466](https://arxiv.org/html/2606.18466))
- The same paper gives MFA 3.0 mean phone-boundary error of 12.1 ms on TIMIT and 13.9 ms on Buckeye. It uses G2P (a Phonetisaurus pair n-gram model through Pynini) for out-of-vocabulary words. It reports no runtime figures and does not test ctc-forced-aligner or Qwen3-ForcedAligner. ([arXiv 2606.18466](https://arxiv.org/html/2606.18466))
- Rousso et al., "Tradition or Innovation" (arXiv 2406.19363, Interspeech 2024), on Buckeye: MFA had 39.8% of word boundaries within 10 ms and 84.9% within 50 ms, MMS 25.0% and 75.0%, WhisperX 18.8% and 67.4%. It compared only words that WhisperX and MMS recognized correctly, which favours the end-to-end systems. ([arXiv 2406.19363](https://arxiv.org/html/2406.19363v1))
- MFA 3.x docs give the English OOV route: `mfa model download g2p english_us_mfa`, then `mfa g2p`, with `--num_pronunciations 3` to get more than the single most likely pronunciation. The docs advise checking generated pronunciations. ([MFA 3.4.0 docs](https://montreal-forced-aligner.readthedocs.io/en/v3.4.0/_sources/first_steps/alignment_example.md.txt))
- Qwen3-ForcedAligner-0.6B pairs the AuT speech encoder with Qwen3-0.6B and fills `[time]` slots non-autoregressively in one pass. Timestamps are indices on an 80 ms frame grid, up to 3,750 classes (300 s). It covers 11 languages including English and was trained on MFA pseudo-timestamps. ([Qwen3-ASR report](https://arxiv.org/html/2601.21337))
- Qwen3-ForcedAligner accumulated average shift (AAS, ms, lower is better): MFA-labeled raw audio 42.9 against NFA 129.8, WhisperX 133.2 and Monotonic-Aligner 161.1. Human-labeled average 32.4 against NFA 101.2. Offline RTF is 0.00889 at concurrency 1 (hardware not given in my extract). The report dates from Jan 2026 and releases the weights under Apache 2.0. ([Qwen3-ASR report](https://arxiv.org/html/2601.21337))
- The model card lists Apache-2.0, "0.9B params" in its safetensors, audio up to 5 minutes, word or character timestamps through `Qwen3ForcedAligner.from_pretrained(...).align(audio, text, language)`, and CUDA in its examples. It says nothing about CPU inference or about text that the audio does not contain. ([HF card](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B)) Community ONNX and CoreML conversions exist. ([ONNX conversion](https://huggingface.co/valoomba/Qwen3-ForcedAligner-0.6B-ONNX), [CoreML conversion](https://huggingface.co/aufklarer/Qwen3-ForcedAligner-0.6B-CoreML-FP16))
- torchaudio's `forced_align` page (2.9.0 docs) says: "This function has been deprecated. It will be removed from 2.9 release. This deprecation is part of a large refactoring effort to transition TorchAudio into a maintenance phase." ([torchaudio 2.9 forced_align](https://docs.pytorch.org/audio/2.9.0/generated/torchaudio.functional.forced_align.html)) Yet the 2.11 multilingual tutorial still shows the MMS_FA bundle in use, so the removal status is unclear. ([torchaudio 2.11 tutorial](https://docs.pytorch.org/audio/2.11.0/tutorials/forced_alignment_for_multilingual_data_tutorial.html))
- ctc-forced-aligner (MahmoudAshraf97): BSD-2-Clause code, default model `MahmoudAshraf/mms-300m-1130-forced-aligner` under CC-BY-NC 4.0, "Atleast 5X less memory usage" than the torchaudio API, word, char or sentence output, and a `<star>` token option (`--star_frequency` segment or edges). It lists no releases. ([ctc-forced-aligner](https://github.com/MahmoudAshraf97/ctc-forced-aligner))
- WhisperX: words whose characters are not in the alignment model's dictionary (its example is "2014.") "cannot be aligned and therefore are not given a timing". ([WhisperX README](https://github.com/m-bain/whisperX)) ParCzech4Speech hit the same limit with digits and spoken symbols (%, &, @). ([arXiv 2509.06675](https://arxiv.org/pdf/2509.06675))
- NeMo Forced Aligner runs Viterbi over CTC log-probabilities and accepts only CTC models or hybrid models in CTC mode, not pure transducers. It uses CUDA when available, else CPU. ([NFA docs](https://docs.nvidia.com/nemo/speech/latest/tools/nemo_forced_aligner.html)) Its Interspeech 2023 paper claimed the best word alignment accuracy and speed among the aligners it compared ([Rastorgueva et al. 2023](https://www.isca-archive.org/interspeech_2023/rastorgueva23_interspeech.pdf)), which conflicts with the 2026 MFA table above, where NeMo trails MFA and MMS. ([arXiv 2606.18466](https://arxiv.org/html/2606.18466))
- NVIDIA's Canary-1B-v2 / Parakeet-TDT-0.6B-v3 paper uses NFA with an auxiliary Parakeet CTC model as its timestamp backbone. ([arXiv 2509.14128](https://arxiv.org/pdf/2509.14128))
- sherpa-onnx's Parakeet TDT 0.6B v2 int8 example output carries per-token `timestamps` such as `[0.32, 0.64, 0.72, 0.80, 0.88, ...]` with `tokens` such as `" Well"`, `","`. The `words` array is empty and no `durations` field appears in that sample. ([sherpa-onnx NeMo transducer models](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-transducer/nemo-transducer-models.html)) The C++ API says `durations` is filled for TDT models. ([sherpa-onnx C++ API](https://k2-fsa.github.io/sherpa/onnx/c-api/html/structsherpa__onnx_1_1cxx_1_1OfflineRecognizerResult.html))
- Parakeet TDT 0.6B v2's card says rare or untrained words are "not likely to be recognized" and that it is "Not recommended for word-for-word/incomplete sentences". ([card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2))

Aligner records

| Aligner | Word-boundary accuracy | Invented names | Size and runtime | CPU | Licence | Released |
|---|---|---|---|---|---|---|
| MFA 3.x | 19.9 to 25.4 ms mean, about 91% within 50 ms ([2606.18466](https://arxiv.org/html/2606.18466)) | needs G2P pronunciations, check them ([docs](https://montreal-forced-aligner.readthedocs.io/en/v3.4.0/_sources/first_steps/alignment_example.md.txt)) | Kaldi-based CLI | CPU, no published speed | not checked | 2016, 3.x current ([2606.18466](https://arxiv.org/html/2606.18466)) |
| MMS via torchaudio MMS_FA or ctc-forced-aligner | 43 to 50 ms mean ([2606.18466](https://arxiv.org/html/2606.18466)) | aligns letter strings, no dictionary (inference from CTC character vocabulary) | 300M wav2vec2, PyTorch ([repo](https://github.com/MahmoudAshraf97/ctc-forced-aligner)) | CPU possible, speed not published | default checkpoint CC-BY-NC 4.0 ([repo](https://github.com/MahmoudAshraf97/ctc-forced-aligner)) | torchaudio API deprecated in 2.8/2.9 ([docs](https://docs.pytorch.org/audio/2.9.0/generated/torchaudio.functional.forced_align.html)) |
| Qwen3-ForcedAligner-0.6B | AAS 32.4 to 42.9 ms on its own tests ([report](https://arxiv.org/html/2601.21337)) | not documented | 0.9B params listed, 80 ms grid, 300 s max ([card](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B)) | not documented, ONNX conversion exists | Apache 2.0 | Jan 2026 |
| NeMo NFA (CTC models) | 78 to 89 ms mean ([2606.18466](https://arxiv.org/html/2606.18466)) | CTC tokens, not documented | NeMo PyTorch | CPU fallback ([docs](https://docs.nvidia.com/nemo/speech/latest/tools/nemo_forced_aligner.html)) | not checked | 2023 |
| WhisperX | about 110 ms mean ([2606.18466](https://arxiv.org/html/2606.18466)) | drops words with characters outside its dictionary ([README](https://github.com/m-bain/whisperX)) | Whisper plus wav2vec2 | not checked | not checked | not checked |
| Parakeet TDT timestamps (sherpa-onnx) | no published figure | timestamps follow the recognizer's own spelling | already loaded | RTF about 0.1 ([sherpa](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-transducer/nemo-transducer-models.html)) | CC-BY-4.0 | 2025 |

### Inferences
- The Parakeet example timestamps are all multiples of 0.08 s, which matches TDT's 80 ms encoder frame. Qwen3-ForcedAligner uses the same 80 ms grid. That is too coarse for phone boundaries but enough for the timing defects in section 3, which are hundreds of milliseconds.
- For timing checks audiogen can probably use the Parakeet token timestamps it already computes. A text-forced aligner adds value where the recognizer misses words. It times the script's own words (including invented names), so a missing word shows up as a squeezed or zero-length span. It is the AlignDPO-style posterior check.
- Of the forced aligners, MMS through ctc-forced-aligner or a hand-written numpy Viterbi over a CTC model is the lightest CPU option and needs no dictionary. Its default checkpoint is non-commercial, which suits a hobby pipeline. Qwen3-ForcedAligner is permissive and newer, but CPU speed is undocumented and it is a 0.9B-parameter LLM. MFA is the most accurate but is a conda and Kaldi install with a G2P step for every invented name.
- Normalize digits and symbols to words before any character-CTC or WhisperX alignment, or those tokens get no timing.

### Gaps
- No published word-timestamp accuracy for Parakeet TDT's own timestamps (sherpa-onnx or NeMo).
- No CPU timings for Qwen3-ForcedAligner, MFA 3.x, NFA or ctc-forced-aligner on English.
- No published test of any aligner on invented fantasy names, or on text the audio does not contain.
- MFA's licence and current version number were not confirmed in what I read.

## 3. Using word timestamps to expose timing defects, with published norms for pauses and speech rate

### Takeaway
The best published read-speech norms are pauses at 94.2% of full stops (mean 734 ms), 69.4% of clause boundaries (652 ms), 44.7% of commas (511 ms) and only 5.7% of unpunctuated phrase boundaries (471 ms). Phrase-final words run about 1.7 to 1.8 times longer than earlier words. The often-quoted 150 to 160 words per minute for audiobooks has no primary source, so per-voice distributions with an interquartile-range rule (as in Emilia-Pipe) are the safer baseline.

### Cited Findings
- Hunt, "Pausing in American English" (BYU MA thesis, April 2023), on 80 read-aloud samples from the International Dialects of English Archive: mean pause 734 ms at periods, 652 ms at clause boundaries, 511 ms at commas and 471 ms at phrase boundaries. Speakers paused at 94.2% of periods, 69.4% of clauses, 44.7% of commas and 5.7% of phrases. ([BYU ScholarsArchive](https://scholarsarchive.byu.edu/etd/10304))
- Liberman's LibriVox analysis (Language Log, March 2017) split 5,832 LibriSpeech chapters (2,484 readers, about 1,571 h) into phrases at silent pauses "greater than 150 milliseconds". In 4-word phrases, mean vowel duration was 85.23, 87.22 and 87.99 ms in the first three words and 155.13 ms in the final word. A commenter's Switchboard figures give 225 ms for non-final words and 383 ms for phrase-final words. ([Language Log](https://languagelog.ldc.upenn.edu/nll/?p=31400))
- An Interspeech 2016 LDC study on LibriSpeech used silences over 200 ms as pauses and found read speech has faster rates and more variable pause and speech-segment durations than spontaneous speech. This comes from a search summary of the PDF. ([LDC Interspeech 2016](https://www.ldc.upenn.edu/sites/www.ldc.upenn.edu/files/interspeech2016-automatic-analysis-phonetic-speech.pdf))
- Emilia-Pipe removes segments with anomalous character or phone durations by interquartile-range outlier detection. ([Emilia, arXiv 2407.05361](https://arxiv.org/html/2407.05361v3))
- AlignDPO uses a "stretch ratio" from a wav2vec2-CTC forced aligner as one of its alignment signals. The extract does not define it further. ([AlignDPO](https://arxiv.org/html/2609.12855v1))
- Audiobook rate: guides cite 150 to 160 wpm as the industry norm without a primary source ([guide](https://sites.google.com/view/snowdaycaculators/audiobook-narration-speed-explained-what-150-160-wpm-really-means)). One AI-narration vendor reports its own English voices at 142 to 185 wpm ([Inkfluence AI](https://www.inkfluenceai.com/audiobook-length-calculator)). I found no peer-reviewed measurement of professional narrators.
- Whisper's `hallucination_silence_threshold` uses word timestamps to skip long silences near suspected hallucinations, so word timing is already used as a hallucination signal in a production recognizer. ([whisper/transcribe.py](https://github.com/openai/whisper/blob/main/whisper/transcribe.py))

### Inferences
- Unexplained pause: a gap of more than about 0.5 s between two words with no punctuation between them is rare in human reading (5.7% of phrase boundaries pause at all, averaging 471 ms). Flagging gaps above 0.5 to 0.6 s with no punctuation is a defensible start. The fix is signal repair (shorten the gap to about 150 to 250 ms) when the words on both sides are intact, else a retake.
- The join's 0.75 s pause cap sits right at the human full-stop mean (734 ms), so it is consistent with read speech. A mid-line unpunctuated pause under 0.75 s passes the cap untouched today.
- Drawn-out word: compare each word's duration per phone (or per character) with the voice's median. Allow about 1.8 times for phrase-final words (before punctuation or at line end), and flag non-final words far beyond that, for example 2.5 times the median. Thresholds need tuning on audiogen's own takes. The fix is a retake.
- Rushed stretch and speech-rate outlier: compute phones or characters per second of speech (pauses excluded) per take and over sliding 3 to 5 word windows. Flag outside the voice's interquartile fences, which is the Emilia method applied per voice. The 1.5 times IQR factor is the usual Tukey choice, not from Emilia's text. The fix is a retake, and a retake with a different seed changes the rate.
- Trailing noise: the time from the last aligned word's end to the take's end, combined with VAD or energy in that tail (section 4), separates harmless silence (trimmed already), a breath or hum (trim at the last word plus about 100 to 200 ms) and babble (speech-like with no matching words, so retake).
- Early cutoff: the last script word missing, or a last word whose duration is far below the voice's norm with the take ending within a few tens of milliseconds of it, suggests a cut. The fix is a retake.

### Gaps
- No corpus study of pause lengths by punctuation in professional audiobooks (as opposed to dialect-archive read passages) turned up.
- No sourced words-per-minute norm for audiobook narration. Per-voice statistics from audiogen's own library are the practical baseline.
- AlignDPO's stretch ratio is not defined in what I read.

## 4. Voice activity detectors for dead air and non-speech sound at the end of a take

### Takeaway
Silero VAD 6.x (MIT, about 2 MB, under 1 ms per 32 ms chunk on one CPU thread) and TEN VAD (June 2025, a 0.3 to 0.7 MB library, RTF about 0.015) both run in sherpa-onnx through the same VAD API. TEN claims sharper speech-to-silence transitions than Silero, which lags by several hundred milliseconds according to TEN. Neither documents a class for breaths, hums or babble, so the useful test is "VAD or energy says audio is present, the aligner says no script word is there".

### Cited Findings
- Silero VAD: "JIT model is around two megabytes in size", "One audio chunk (30+ ms) takes less than 1ms to be processed on a single CPU thread", and "Under certain conditions ONNX may even run up to 4-5x faster". It supports 8 kHz and 16 kHz. ([silero-vad README](https://github.com/snakers4/silero-vad))
- Silero releases on PyPI: 6.0.0 on 26 Aug 2025, 6.2.0 on 6 Nov 2025, 6.2.3 (current) on 23 Sep 2026. The licence field says MIT while one badge says CC BY-NC 4.0. ([PyPI silero-vad](https://pypi.org/project/silero-vad/))
- Silero `get_speech_timestamps` defaults: `threshold=0.5`, `min_speech_duration_ms=250`, `min_silence_duration_ms=100`, `speech_pad_ms=30` ("Final speech chunks are padded by speech_pad_ms each side"), `window_size_samples=512`, and an optional `neg_threshold` exit threshold. ([utils_vad.py](https://github.com/snakers4/silero-vad/blob/master/src/silero_vad/utils_vad.py))
- A Habr comparison that links the v6.0 release reports v6 at 0.65 against 0.61 for v5 on the ESC-50 noise set, and 0.53 against 0.44 on private noise. It lists music with voice-like instruments and very high voices as known weak spots. This is a secondary source. ([Habr](https://habr.com/en/articles/940750))
- TEN VAD: ONNX model and preprocessing open-sourced 2025/06, sherpa-onnx integration 2025/07. Licence is Apache 2.0 "with additional conditions". It takes 16 kHz input with 160 or 256 sample hops (10 or 16 ms) and returns a per-frame probability in [0, 1] with a default threshold of 0.5. RTF 0.0160 on an M1, 0.0150 on an i7-10710U, and 0.0086 against Silero's 0.0127 on a Xeon Gold 6348. The README claims "Silero VAD suffers from a delay of several hundred milliseconds" at speech-to-non-speech transitions and that TEN shows "superior precision", but it prints no precision numbers. ([TEN VAD](https://github.com/TEN-framework/ten-vad))
- A third-party Rust crate's table puts Silero at precision 0.938 / recall 0.938 and TEN at 0.942 / 0.915, with near-equal RTF (0.0038 and 0.0039), on an unnamed dataset and different frame sizes. ([wavekat-vad README](https://docs.rs/crate/wavekat-vad/0.1.8/source/README.md))
- sherpa-onnx's VAD config has a `sileroVad` block and a `tenVad` block with the same fields (model, threshold, min speech and silence durations, window size). Setting `tenVad.model` and leaving `sileroVad.model` empty switches models. ([sherpa-onnx VAD API](https://k2-fsa.github.io/sherpa/onnx/javascript-api/examples/api_vad.html))
- pyannote segmentation-3.0: MIT, gated on Hugging Face, 10 s mono 16 kHz windows, a seven-class powerset output including non-speech, and usable as a VAD pipeline. ([HF card](https://huggingface.co/pyannote/segmentation-3.0))
- Whisper large-v3 hallucinates text on 40.3% of non-speech clips ([arXiv 2501.11378](https://arxiv.org/abs/2501.11378)), so a Whisper transcript of a take's tail is not evidence of babble.

VAD records

| VAD | Detects | Published accuracy | Size | CPU speed | Format | Licence | Released |
|---|---|---|---|---|---|---|---|
| Silero VAD 6.x | speech or not, per 32 ms window | vendor wiki only, v6 noise gains secondhand ([Habr](https://habr.com/en/articles/940750)) | about 2 MB ([README](https://github.com/snakers4/silero-vad)) | under 1 ms per chunk on one thread ([README](https://github.com/snakers4/silero-vad)) | JIT, ONNX, sherpa-onnx | MIT (badge conflict) ([PyPI](https://pypi.org/project/silero-vad/)) | 6.0.0 Aug 2025, 6.2.3 Sep 2026 ([PyPI](https://pypi.org/project/silero-vad/)) |
| TEN VAD | speech or not, per 10 or 16 ms hop | PR curves only, no numbers ([TEN](https://github.com/TEN-framework/ten-vad)) | 306 KB Linux, 508 KB Win x64 library ([TEN](https://github.com/TEN-framework/ten-vad)) | RTF 0.016 on M1 ([TEN](https://github.com/TEN-framework/ten-vad)) | ONNX, C library, sherpa-onnx | Apache 2.0 with extra conditions ([TEN](https://github.com/TEN-framework/ten-vad)) | June 2025 ([TEN](https://github.com/TEN-framework/ten-vad)) |
| pyannote segmentation-3.0 | speech, overlap, non-speech | not on card | not on card | not on card | PyTorch | MIT, gated ([card](https://huggingface.co/pyannote/segmentation-3.0)) | not on card |

### Inferences
- For dead air, the aligner's last word end plus an RMS envelope is more exact than a VAD segment end. Silero pads segments by 30 ms and, according to TEN, lags at offsets. A VAD adds value for telling "silence" from "something there".
- A tail where the VAD fires for more than about 300 ms after the last aligned word and the recognizer finds no extra words is likely a breath, hum or babble. Trimming at the last word plus a short tail is a safe automatic fix. If the recognizer finds extra words there, it is content (an insertion), so retake.
- sherpa-onnx is already in the audio Python, and both its VADs are tiny CPU models, so a tail check adds no new dependency and no VRAM.
- pyannote is heavier, gated and built for diarization. It brings nothing a tail check needs.

### Gaps
- No VAD documents how it classes breaths, hums or laughter, and I found no benchmark of VADs on TTS output.
- No independent, numeric Silero against TEN comparison on a named public dataset.
- I did not find a sound-event model evaluation for breath or hum detection within this scope (the artifact researcher may cover it).

## 5. Generating N candidate takes and picking the best: evidence and returns

### Takeaway
Best-of-N with a recognizer verifier gives real but modest corpus-level gains on a strong model. F5-TTS went from 2.06% WER to 1.88% (N=3), 1.80% (N=5) and 1.72% (N=10) with Distil-Whisper large-v3 as verifier, and the paper says the literature reports 10 to 30% relative. Gains shrink or vanish with a weak verifier, and they are overstated when the verifier and judge share a model family. Llasa shows that selecting by speaker similarity alone can make WER worse, while adding a WER verifier cut its hard-set WER from 12.13% to 5.24%.

### Cited Findings
- Yu and Kang (arXiv 2607.08256, v2 5 Aug 2026), F5-TTS on LibriSpeech-PC test-clean (1,127 samples), judged by faster-whisper large-v3: baseline 2.06%. Distil-Whisper large-v3 (756M) verifier: 1.88% at N=3, 1.80% at N=5, 1.72% at N=10. wav2vec2-base (95M) verifier: 1.99%, 2.20% and 1.98%. An oracle reaches 1.42% at N=10. ([arXiv 2607.08256](https://arxiv.org/html/2607.08256))
- At N=3 only the Distil-Whisper large-v3 verifier gave a significant reduction (-8.7%, p=0.030). A three-way rank ensemble gave -7.2% (p=0.057), wav2vec2-base -3.5% (p=0.31) and Distil-Whisper small -1.0% (p=0.93). ([arXiv 2607.08256](https://arxiv.org/html/2607.08256))
- The same paper's recommendation is a cross-family rank average or max-rank ensemble at N=5 ("the best compute-robustness frontier"). Its best ensemble at N=10 gives a 12% relative reduction averaged over three evaluators (1.61% against 1.83%), with no measurable SIM-o or UTMOS loss. It states that BoN is reported to cut WER by 10 to 30% relative across recent flow-matching systems. ([arXiv 2607.08256](https://arxiv.org/html/2607.08256))
- Llasa (Ye et al., arXiv 2502.04128, Feb 2025) scaled inference compute with verifiers: WavLM speaker similarity for partial process-reward search (16 candidates per beam step) and Whisper-large-v3 WER for the final outcome-reward pick. For Llasa-1B-250k, test-en WER went from 3.22 to 1.46 and test-hard WER from 12.13 to 5.24. SIM-only search left WER "poor, especially for larger beam widths, sometimes lagging behind the baseline." ([Llasa](https://arxiv.org/html/2502.04128v2))
- Koel-TTS used 6 samples per text and speaker pair (top-k 80, temperature 0.7), ranked by Parakeet TDT 1.1B CER and TitaNet similarity, to build preference pairs. ([Koel-TTS](https://arxiv.org/html/2502.05236v1)) AlignDPO used 20 to 30 candidates per pair. ([AlignDPO](https://arxiv.org/html/2609.12855v1)) Both used candidates for training data rather than for inference-time selection.
- "Taming Long-form TTS" fixes a detected failure by rolling back, reseeding and constraining attention, with capped retries. Detect-then-resample is the published alternative to always-on best-of-N. ([arXiv 2609.16989](https://arxiv.org/html/2609.16989v1))

### Inferences
- audiogen's current policy (resample only flagged takes, up to two rounds) matches detect-then-resample. With 17 of 7,602 takes flagged (0.22%), always-on best-of-3 would triple Breeze render time for gains that the literature measures in tenths of a WER point at corpus level. That looks like poor value for a home GPU.
- A cheaper middle ground: when a take is flagged, render 2 or 3 retakes at once and keep the best by a rank average of Parakeet and Qwen3-ASR (two families), instead of taking the first retake that passes. That follows the paper's cross-family ensemble advice at small N on exactly the takes that need it.
- Use a different recognizer to audit than the one that selected. If Parakeet both picks and scores the take, its WER will overstate the gain (the family-alignment confound).
- Never select on speaker similarity alone. Llasa shows that can trade away content accuracy.

### Gaps
- No published curve beyond N = 3, 5 and 10 for recognizer-verified selection, and no split of how much of the gain comes from fixing catastrophic failures against small errors.
- No best-of-N study on Breeze TTS or any GGUF llama.cpp-style TTS, and none at temperature 0.9.
- No published result for verifier ensembles that include Parakeet or Qwen3-ASR.

## 6. Checks for lines of one to three words

### Takeaway
Per-line WER is close to meaningless at one to three words, since one substitution in a one-word line is 100% WER. Published practice switches to exact match with a tiny edit allowance (AlignDPO: one edit under ten words, any deletion fails) and to perfect-match rates. Parakeet's model card warns against "word-for-word/incomplete sentences". The workable checks for short lines are duration per phone, forced-alignment posteriors of the script text, tolerance at character or phoneme level for names, and agreement between two recognizer families.

### Cited Findings
- AlignDPO's rule for short texts: exact word match, "one edit tolerated for texts under ten words and any deletion forcing" a fail. ([AlignDPO](https://arxiv.org/html/2609.12855v1))
- PashtoTTS-Bench (arXiv 2605.26978) reports the share of utterances with zero WER ("Perfect%") and with WER of 10% or less ("Low-error%") beside mean WER. ([PashtoTTS-Bench](https://arxiv.org/pdf/2605.26978))
- Parakeet TDT 0.6B v2 card: "Not recommended for word-for-word/incomplete sentences", and rare words are "not likely to be recognized". ([card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2))
- In conversational speech, recognizer errors increase for short words, infrequent words and fast speech (Goldwater et al., 2010). ([Goldwater et al.](https://nlp.stanford.edu/~manning/papers/goldwater10.pdf))
- Whisper large-v3 produces text for 40.3% of non-speech inputs ([arXiv 2501.11378](https://arxiv.org/abs/2501.11378)), which matters for short takes that are mostly silence.
- sherpa-onnx hotwords (contextual biasing) work only with `modified_beam_search` on transducer models, with a boost score per phrase, and need tokens made by `sherpa-onnx-cli text2token`. ([sherpa hotwords docs](https://github.com/k2-fsa/sherpa/blob/master/docs/source/onnx/hotwords/index.rst)) NeMo itself offers GPU phrase boosting for CTC, RNN-T/TDT and AED models. ([NeMo customization docs](https://docs.nvidia.com/nemo/speech/latest/asr/asr_language_modeling_and_customization.html))
- The AlignDPO detector over-flags low-confidence rare words ([AlignDPO](https://arxiv.org/html/2609.12855v1)), so an alignment posterior alone will often fail a correct invented name.

### Inferences
- For one to three word lines (shouts, names, "Yes."), a practical cascade is:
  1. Normalized exact match with the recognizer: accept.
  2. Otherwise compare at character level (or phoneme level if a G2P is added). A small distance on a name token means the recognizer's spelling, so accept. A missing word or a large distance goes to step 3.
  3. Second recognizer from another family (Qwen3-ASR). If it matches, accept. If both fail, retake.
- Duration is the most reliable short-line signal. Bound the speech span (first to last aligned word, from the timestamps) by syllable or phone count times the voice's per-phone duration range, not by word count. One-word lines vary far more per word than long lines. Silence padding is already trimmed by the join, so measure only the speech span.
- A forced-alignment posterior of the exact script text (mean per-word posterior, plus a stretch ratio as in AlignDPO) helps with "was anything extra said" (extra speech outside the aligned span), even where the recognizer is unreliable. Treat a low posterior on an invented name as weak evidence.
- Hotword biasing toward the cast's names could cut the name noise in the take check. It needs modified beam search, and sherpa-onnx support for the Parakeet TDT export is unconfirmed. Bias only the names, never the whole line, or the check would hear what it expects.

### Gaps
- No published study of automatic content checks for one to three word TTS lines turned up. Seed-TTS-eval, CV3-Eval and LibriSpeech-PC test sets are sentence-length.
- Whether sherpa-onnx hotwords work with the Parakeet TDT 0.6B v2 export is unconfirmed.

Cloud alternatives (cost per finished hour of audio, transcription only)
- AssemblyAI lists Universal-2 batch at about $0.15 per hour and Universal-3.5 Pro batch at about $0.21 per hour (vendor blog). ([AssemblyAI pricing blog](https://assemblyai.com/blog/speech-to-text-api-pricing))
- Deepgram Nova-3 is listed at $0.0043 per minute batch (about $0.26 per hour) and $0.0077 per minute streaming by one 2026 aggregator, while other 2026 aggregators quote $0.0077 per minute for batch. ([convertaudiototext on Nova-3](https://convertaudiototext.com/blog/deepgram-nova-3-explained))
- Inference: as a second-opinion recognizer, a cloud API would cost roughly $0.15 to $0.50 per finished hour if run on every take, and well under a cent per hour if run only on the 0.2 to 1% of takes that Parakeet flags or marks suspect. I did not check word timestamps or confidence output for these APIs.
