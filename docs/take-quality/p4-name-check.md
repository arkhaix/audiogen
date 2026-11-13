# P4, continued: refining the name check

This is an addendum to `phase-2.md`, package P4. The name check is live in its second version, and it is right when it flags and misses part of what AJ hears. This file says what is known, what is open, and the order to work in. Start it with "continue the name check in docs/take-quality". `DECISIONS.md`, "The take check flags a name when both recognizers hear a sound its spelling doesn't have", has the rule and why the first version went.

## Where the check stands

As of 2026-11-13:

* The rule is `phones.adds()`. A take is flagged `name` when Parakeet's spelling of a name and ZIPA's phones in its place both have a sound that the say-as spelling doesn't have. It reads skeletons, the consonants in order with one mark for each run of vowels, so it hears an added syllable or consonant and nothing else.
* It judges only a name that has a say-as. On containers that is Eole as "Ay-ohl" since 13:52 on 2026-11-13 and "Eh-ole" before, Vouivre as "Vweevra", Louis as "Lewis", and Lauriane and its second spelling Laurianne as "Laurie Anne".
* AJ has marked 169 clips on four listening pages, numbered 1, 2, 3 and 5, and could judge 167. The rule agrees with him on 129. Of the 25 it flagged on the second page, 23 sound wrong to him. Of the 29 it passed there, 8 sound wrong. Of the 44 it passed on the third, 26 sound wrong, and all 26 are one fault it can't hear, the wrong vowel. Of the 30 takes of "Éole" it passed on the fifth, one sounds wrong.
* By kind of flag, on the second page: a vowel after the name 11 of 11 wrong to him, an h inside the name 4 of 4, a sound before the name 2 of 3, and other added sounds 6 of 7.
* With "Eh-ole", after a take check of all 40 chapters that have names, 140 of the 345 takes that say Eole added a sound, 6 of 157 for Vouivre, and none of 140 for Louis and 186 for Lauriane.
* With "Ay-ohl", after AJ's backlog render of the 31 chapters that say Eole, none of its 344 takes adds a sound, and none of 156 for Vouivre, 140 for Louis and 186 for Lauriane. No chapter has a name finding, so the breaker held nothing. That is what the rule says of the takes.
* What AJ hears in them, from the third page on 2026-11-13: "Vweevra" is right in 14 clips of 14. "Ay-ohl" is right in 4 of 30. The other 26 say "eye-ohl", and both recognizers have it: ZIPA's first vowel is `aɪ`, or `ɪ` or `i` in three, and Parakeet writes "aiol", "ayol" or "iol". In the 4 he accepts ZIPA has `eɪ` and Parakeet "aol". By both recognizers together, 268 of 312 sayings of "Ay-ohl" in the library say "eye" first, 86%, against 4 of 321 with "Eh-ole". So "Ay-ohl" is the worse spelling to his ear: about one take in eight is right, where about half were with "Eh-ole", which adds a sound in four takes of ten and is right in most of the rest.
* The screen could not have shown it. It counts added sounds, and "Ay-ohl" was steady at the wrong vowel. Its own eight takes had `aɪ` first in six.
* The option screen counts with the same rule. AJ screened Eole's seven options with it on 2026-11-13 at 13:38, and N1 below has the counts. He picked "Ay-ohl" at 13:52.
* The two breeze nodes give the same verdict. Both heard the same 100 takes on 2026-11-13, 60 that say Eole, 20 Vouivre and 10 each Louis and Lauriane, and they agree on 99: both flag 23 of Eole and 2 of Vouivre. Their transcripts differ on 18 takes and their phones on 46. The one take they judge differently is a possessive of Eole that Parakeet wrote as "Ehol's" on the laptop, which flags it, and as "Iol's" on the Mac.

## What is not known

* What a retake for an added sound puts in the take's place. The 12 retaken lines on the third page were takes of "Ay-ohl", whose fault is the vowel, so they don't answer it. 4 of the 12 sound right to AJ and none of the 18 that were not taken again, which is unexplained.
* How many first takes of "Ay-ohl" added a sound. 19 lines that say it were taken again after a flag, and a render records the lines and not the reasons, so it is 19 in 344 at most.
* Whether a spelling that screens seven of eight comes out clean in a book's chapters. "Eh-ole" screened 8 of 8 by the first version and 7 of 8 by the second, and 59% of its takes in the library were clean. "Ay-ohl" screened 8 of 8 and its 344 takes are clean. So 8 of 8 held up once, and 7 of 8 did not.
* Which spelling of Eole the voice says right most often. No option so far is both steady and right.
* How the rule does on a name it was not shaped on. Almost every flag so far is on Eole.
* Whether the rule can hear a fault in a say-as that is a real word, such as "Lewis" or "Laurie Anne". ZIPA writes a real word as the dictionary reads it, whatever the take says (`DECISIONS.md`, "A heteronym gets no respelling and no check"), so its phones may leave out a sound such a take adds. Nothing shows a miss: the 326 takes of Louis and Lauriane have no flag, and AJ accepted all 24 clips of them he marked. No name was tested for it.

## Rules for this work

* A rule that judges how something sounds goes live only after AJ has marked a listening page for it. The first version passed every test and agreed with his ear on 23 of 40 clips.
* Draw a page at random within what the rule flags and within what it passes, from sayings that were on no earlier page. Hide what the rule says of each clip. Keep the count each sample was drawn from, so the marks give how often a flag is right and how often a pass is wrong.
* Marks that shaped a rule are not a test of it. All 94 have shaped the rule by now, so each change needs fresh clips.
* A flag needs two witnesses of different kinds. One recognizer's phones gave 10 false flags in 17.
* Compare what the ear compares. AJ lets a vowel differ a good deal in Eole and rejects an added syllable or consonant. Read new marks for the kind of difference he rejects before choosing a measure.
* Measure before building. Stop a step when the fault it is after turns out to be rare.
* Work in a scratch copy, never on AJ's chapters (`CLAUDE.md`, Testing). A page takes him five to ten minutes, so ask for one only when a decision hangs on it.
* Bump `JUDGE_V` when the rule changes and `pronounce.SCREEN_V` when what a screen counts changes.

## Steps, in order

### N0: the marks as a scored set

* Done on 2026-11-13. The marks are the only labelled data for this, and they are in the repo now: `tests/data/name_marks.jsonl` has one record for each marked saying, 95 from the first two pages and 169 since the fifth.
* A record has `page` and `clip`, which name the saying on its listening page, and `term`, `chapter`, `line` and `voice`. `said` is the name's words as the voice was sent them, `heard` is Parakeet's spelling, and `before` and `after` are the line's words on either side. `phones` and `starts` are ZIPA's phones within half a second of the saying's span, which runs from `t0` to `t1`. `mark` is `y` for a saying AJ hears as picked, `n` for one he hears another way, and `u` for the one he couldn't judge.
* `PhonesTest.test_the_rule_is_scored_on_the_sayings_aj_marked` in `tests/test_asr.py` reads the phones as `takecheck.name_spans()` does and pins the score: 83 of 94 on the first two pages and 129 of 167 over all, with the sayings the rule passes and he rejects and the two it flags and he accepts, each by page and clip. A change to the rule or to `NAME_NEAR` shows there as the clips it wins and loses.
* Add the records of a new page to the file under the next page number. A record is what `takecheck.name_spans()` reads of the take, with the symbols and times of the take's `phone` block within half a second of the span. `n0_build.py` in `D:\fish.wt\scratch\n7` built the file from the two pages' keys and answers, and it goes when the scratch folder does.

### N1: watch the first live use

* Done on 2026-11-13. AJ screened Eole's options on the Names tab at 13:38, picked "Ay-ohl" at 13:52 and queued a backlog render of the 31 chapters that say Eole.
* The screen: AJ's job of 13:38 said the seven options eight times each, 56 takes, in 4 min 39 s on aj-legion5, and nothing failed. "Ay-ohl" and "Ayohl" came out clean 8 of 8, "Eh-ole" and "Ey-ole" 7 of 8, the name as written and "Eh-ohl" 5 of 8, and "Ay-ole" 3 of 8. The three under seven are not offered.
* "Eh-ole" is the spelling the library's takes were made with, and 59% of them are clean. A spelling at 59% screens seven of eight or better one time in ten, so one screen can't tell it from the two at 8 of 8. That is the first evidence for N6.
* The renders: the batch ran on aj-legion5 from 13:52 to 15:43, 31 chapters at a median of 3.4 minutes, and the longest, chapter 21 with 56 takes to make, took 7.4. It made 476 takes, more than the lines that say Eole, because a chapter also got the lines whose text had changed since its last render and its flagged takes again. It took 86 lines again after a flag and kept a new take for all but five, which are still flagged for `swing` or `drift`. A deploy restarted the app at 14:37 while chapter 22 rendered, and the batch went on.
* The name: none of the 344 takes that say "Ay-ohl" adds a sound, in any chapter, and the breaker held nothing. 19 of those lines, in 11 chapters, were taken again after a flag. `after.py` in `D:\fish.wt\scratch\n1` reads all of this from the library, `after_eole.jsonl` there is its output, and `retakes.py` reads the job logs.
* A fault it showed: a render logs the lines it takes again and not the reasons, and the check file keeps only the take that was kept. So the batch can't say how many first takes of a name were flagged for the name. "Open ends from the build" has it.
* The screen against the book: "Ay-ohl" screened 8 of 8 and all 344 of its takes are clean. "Eh-ole" screened 7 of 8 and 59% of its takes were clean. One screen of eight told the two apart by a single take, which is the case for N6.
* The nodes: done on 2026-11-13, and "Where the check stands" has the result. `D:\fish.wt\scratch\n1` on the laptop has the 100 takes, each node's hearing and the scripts. `pick.py` draws the takes from the p4 copy, `hear_list.py` hears them as the take check does, `run_mac.sh` runs it on jsc-m1 with the node's settings, and `compare.py` judges both hearings.

### N2: a third listening page

* Done on 2026-11-13. AJ marked the page the same afternoon: https://claude.ai/artifact/5PESpbqEgyAa4XNzCEtCpK, "Name Listening Check 3". It has 44 clips from the batch's chapters, drawn at random: 12 of the 19 lines that say Eole and were taken again after a flag, 18 of the 325 other takes of "Ay-ohl", and 14 of the 148 takes of "Vweevra". The rule passes all 44, so every clip he marks as another way is a miss of the rule.
* `D:\fish.wt\scratch\n2` on the laptop has the library as it was after the batch, `sample3.py`, which drew the takes, `cut3.py`, which cut the clips and wrote the page, and `listen\key3.json`, which maps each clip to its take and its group. His answers are beside it as `answers3.txt`, and the 44 sayings are page 3 of `tests/data/name_marks.jsonl`, added by `page3_records.py`. `marks3.py` lists each clip with his mark and both recognizers' hearing, and `vowel.py` counts the first vowel in the marks, the library and the options.
* His marks: "Vweevra" as picked in 14 of 14, so the vowel fault of Vouivre is gone. "Ay-ohl" as picked in 4 of 30, and the 26 others say "eye-ohl" ("Where the check stands"). The 4 are all among the 12 lines that were taken again.
* What the marks call for: N4, because the fault is a vowel, in Eole and not in Vouivre, and N6, because the screen passed it 8 of 8. N3 waits for the spelling Eole ends up with. First of all Eole needs a spelling that is right, which the next point covers.
* A spelling for Eole, tried in `D:\fish.wt\scratch\n2` on 2026-11-13 and still open. `screen.sh` screened seven more spellings there, eight takes each, and `screen1.log` has the counts. Read by ZIPA's first vowel together with the rule, the takes with the right vowel and no added sound are 6 of 8 for "Eh-ole", 5 for "Éohl", "Eh-oal" and "Eyohl", 4 for "Ey-ole" and "Eh-ohl", 3 for "Éole", 2 or fewer for "Eigh-ohl", "A-ohl", "Ayy-ohl", "Ayohl" and the name as written, and none for "Ay-ohl" and "Ay-ole". Every spelling that starts with "Ay" or "A" is mostly said "eye". No new spelling beats "Eh-ole", whose fault is the added sound, which the rule does hear.
* So the trial was "Eh-ole" with its flagged takes taken again. `render_free.py` rendered chapter 38 in the scratch copy with the breaker off: 13 of the 30 lines that say Eole added a sound on the first take, two rounds of retakes kept a clean take for 9, and 4 are still flagged (`render38.log`). That is 26 of 30 clean by the rule, against 17 without retakes.
* A fourth page has those 30 takes and waits for AJ's marks since 16:30: https://claude.ai/artifact/2L3hYbQvtkYdeBs1ndoT1X, "Name Listening Check 4". `sample4.py` listed the takes, `cut4.py` cut them, and `listen\key4.json` has each clip's take and its group: 17 first takes the check passed, 9 kept after a retake and 4 still flagged. Put his answers beside it as `answers4.txt` and add them to `tests/data/name_marks.jsonl` as page 4.
* AJ asked at 17:00 whether a spelling with an accent was tried. "Éole" got the same trial: 5 of the 30 lines added a sound on the first take, one round of retakes kept a clean take for all 5, and none is flagged (`render38-eole-accent.log`). Its screen had said 5 of 8, so eight takes misjudged it the other way. By the recognizers its first vowel is as "Eh-ole" has it. A fifth page has those 30 takes, the same lines as the fourth, and waits for his marks since 17:10: https://claude.ai/artifact/DC8Y4so8BCA6Sdc4WVpbPr, "Name Listening Check 5". `cut5.py` cut it, `listen\key5.json` is its key, and `keep\ehole38` and `keep\eoleaccent38` hold each trial's chapter folder, because the second render replaced the first one's takes.
* AJ marked the fifth page at about 17:15: "Éole" is as he wants it in 28 clips of 29, with one he couldn't judge. The one he rejects is line 158, which Parakeet wrote as "ayule", and the one he couldn't judge is line 195, written as "ill". His answers are `listen\answers5.txt`, and `page_records.py` added the 30 sayings to `tests/data/name_marks.jsonl` as page 5. He sent one line of 30 answers after both pages were out, and it is read as the fifth page's because it fits that page's takes: the fourth page's four flagged takes would all be marked right otherwise.
* So "Éole" is the spelling to use: right in 28 of 29 where "Ay-ohl" was right in 4 of 30. The fourth page, "Eh-ole" with retakes, has no marks and needs none now.
* AJ set Eole to "Éole" at 17:21 and queued the backlog render of the 31 chapters. Chapter 48 was rendered at 17:25 with the breaker as it was. He then paused the queue, so the other 30 wait for the deploy and run without the breaker, and chapter 48 is rendered again after it.
* He decided at 17:22 that a render always takes a name's flagged takes again (`DECISIONS.md`, the name rule's entry, its last point). It was deployed at 18:00 with phase 3's emphasis work, and the queue went on at 18:01. `takecheck.name_findings()` is gone, `takecheck.voice_findings()` leaves the reason `name` out, and `breeze.retake_flagged()` holds a take only for a voice finding. `breeze.flagged_before()` counts a take that an earlier render held for a name as flagged, so the next render of its chapter takes it again. After the batch, list the chapters that still have a take flagged for a name. Those takes have been through both retake rounds, and a render doesn't take them a third time.
* It was cut from three groups: takes that a render kept after a retake, takes of "Ay-ohl" that the rule passes, and takes of "Vweevra".
* It answers whether a retake gives a take he hears as right, whether the new spelling is right when the rule says so, and whether the vowel fault of Vouivre is gone.
* A retake replaces the take's file, so the take it replaced can't be on the page. Copy flagged takes out of the scratch render first if a before and after is wanted.
* The next three steps depend on these marks. Do only the ones the marks call for.

### N3: the added vowel that the spelling hides

* The rule lets a take pass when the vowel after the name could be the next word's: in a possessive, and before a word that starts with a vowel or an h. Three of the 15 Eole clips it passed on the second page are of this kind.
* Lead for possessives, from three clips: the two takes Parakeet wrote as "aeole's" sound wrong to AJ, and the one it wrote as "aeol's" sounds right. Parakeet writes "lorianne's" for every take of Lauriane, so the final e tells something only when Parakeet's usual spelling of the name lacks it. The check files hold every spelling of every name, so the usual one can be counted.
* ZIPA's vowel alone is no sign in a possessive. It writes a vowel before the s in takes that sound right too: `eɪlɛz` for a take of "Eh-ole's" that AJ accepted, and `ænɛz` in 7 of the 22 possessives of Lauriane.
* Count first how many sayings are possessives or stand before a vowel or an h. Stop if they are few once Eole has a steadier spelling.

### N4: a vowel that only sounds different

* Five of the 6 Vouivre clips the rule passed sound wrong to AJ. They end in "vroo" or "vroh" where he wants "vruh".
* Both recognizers heard it. Parakeet wrote "vouivreux", "vouivru", "vivrew" and "vui vro" for those and "vivra" for the one he accepted, and ZIPA has `u` or `oʊ` for them and `ə` for his.
* A rule would compare the class of a vowel, and only where both recognizers agree on a class the spelling doesn't have. Reading a vowel's class from English letters is less sure than reading consonants. The other way is to hold each recognizer to its own hearing of the clip AJ picked, which needs the options job to keep that clip's phones.
* The risk is the first version's: ZIPA's vowels alone were not what he hears. His ear also accepts `eɪl`, `iɔl` and `eoʊl` for "Eh-ole", so the rule can't treat every vowel alike. The marks so far point at the last vowel of the name.
* A second risk came out of P5 on 2026-11-13. ZIPA wrote "live" with the long vowel in 21 takes of 22 that say the short one, because it writes a real word as the dictionary reads it. An invented spelling such as "Vweevra" or "Ay-ohl" has no dictionary reading, so ZIPA's vowel may still be evidence there, and for a say-as that is a real word it is not. `D:\fish.wt\scratch\p5` has a check that hears a vowel from the audio, which was 96 to 99% right on held-out words.
* N2 showed the fault in another name on 2026-11-13, so this step is called for. "Ay-ohl" is said "eye-ohl" in 26 of the 30 clips AJ marked, and the first vowel by both recognizers parts his marks 29 times in 30: `aɪ`, `ɪ` or `i` with a spelling that starts "ai", "ay" or "i" for the clips he rejects, and `eɪ` with "ao" for the four he accepts. ZIPA's first vowel alone parts all 30.
* That shape was read off those 30 marks, so they are not its test. Pages 1 and 2 can't test it either: one clip of "Eh-ole" there has "eye" first by both, and he rejected it. A rule needs a fresh page with both kinds, and it has to say which vowel a spelling means: "Ay" is the vowel of "day" to AJ and of "eye" to the voice.
* The screen needs it more than the take check does. A take check with it would have flagged nine takes of "Ay-ohl" in ten after the renders. A screen with it would have shown the spelling as wrong before the pick.

### N5: names without a say-as

* 158 of the 162 names the Names tab lists have no say-as, and the rule doesn't judge them, because an English spelling often has a skeleton the voice doesn't say. The prototype flagged three of four takes of Goetia, which the voice says right.
* Lead: hold a name to its own usual skeleton in place of its spelling. A take in which both recognizers add a sound to what most takes of the name have is the outlier that P4 set out to retake. A name that is said wrong every time still needs AJ's pick, and the Names tab already lists it.
* The usual skeleton has to be stored where `judge()` can read it, and kept fresh as takes change. The first version's `said` and `pronounce.hold()` did that for a reference saying and can be the model.
* Measure first, on a scratch copy of the library's check files, which now hold a phone block for every take: how many takes of a name without a say-as would this flag, and for which names. Then cut a page from them before any of it is built.

### N6: the screen

* Eight takes are coarse. A spelling that nine takes in ten say clean passes four screens in five, and one at six in ten passes one in ten. A count of six of eight decides little. Have the job say such an option eight times more by itself.
* The screen can't hear a vowel fault. If N4 gives a rule, the screen counts with it.
* Use the comparison from N1 to set the share an option needs, in place of 85%.

### N7: judging saved takes faster

* Done on 2026-11-13 (`DECISIONS.md`, "Judging a take again takes a fifth of the time, and the Names tab still reads the checks"). Judging the library's saved takes again, 8,918 with evidence in 40 chapters, went from 82 s to 18 s on the laptop, 2.1 ms a take, and every take gets the result it got before.
* The profile put 60% of the time in the word alignment, 21% in `voice_text()` and 13% in the word times. The token map was not most of it.
* It was deployed on 2026-11-13 at 13:45. The app's container judges the library's saved takes again in 9 s.
* The Names tab still counts only chapters whose check is current. The library would hold a request for those 9 s, so the tab still can't judge on one. After a bump of `JUDGE_V`, queue a backlog take check of the chapters, which hears nothing again when the blocks are current.
* What is left is spread, about a quarter each in the alignment, the token map and the word times. A faster judge would need the token map of a line and the word times of a take worked out once and handed to every rule that reads them.

## Not planned

* A sound the take drops. The rule ignores it because both recognizers drop sounds, and AJ accepted "ay-oh" without its l.
* A forced alignment of the line's phones, which would place a name better than Parakeet's word times do. It needs a pronouncing dictionary that neither audio Python has.
* A better English reading in `phones.spelled()` for its own sake. Fix a reading when a false flag shows it wrong, and add the case to the test.

## Open ends from the build

* The Codex review of the second version did not run, because the account was at its usage limit. The Opus blind review found no blocking defect. The change is commit 28e709c. A second try on 2026-11-13 at 13:05 met the same limit, which the account said would lift at 14:07. Run the codex-review skill on that commit in a later session.
* Steps N0 and N7 had the Opus blind review alone, for the same reason. It found no blocking defect. Two odd inputs behave differently from before and are left so: `voice_text()` gives a bible whose term is the YAML value true the names of an otherwise equal bible whose term is 1, when both are mapped in one process, and `align()` raises on a word that can't be hashed, where `word_edits()` only ever passes text.
* A render doesn't record why it took a line again. Its log names the lines, `render.check.retaken` lists them, and `take_check.json` holds the kept take alone. Record each retaken line's reasons, so a batch shows how many first takes a name flagged.
* A say-as that the rule reads wrong has each of its takes taken again twice, once, since no breaker holds a name any more. The Names tab then shows the spelling as unsteady. No say-as in the library is read wrong today.
* A take made with an earlier say-as is judged against the new one until its chapter is rendered. The Names tab's count for a name therefore describes the old takes until then.

## What AJ does

* He screened Eole's options, picked "Ay-ohl" and queued the renders on 2026-11-13.
* He marks a listening page when a step asks for one. N2's is the next.
* He decided on 2026-11-13 that a name's flagged takes are always taken again.
* He decides whether names without a say-as should be judged at all, once N5 has its numbers.

## Data and tools

* `D:\fish.wt\scratch\p4` on the laptop has the scratch copy: the library as containers had it on the morning of 2026-11-13, the takes under `media`, and ZIPA's phones for the 826 takes that say Eole, Vouivre, Louis or Lauriane in `phones.jsonl`. Chapter 22 there was rendered again in scratch, so leave it out.
* `spans.py` writes one record for each name in each take, with the words on either side, into `spans_all.jsonl`. `skel.py` is the prototype of the rule, and `v2_same.py` checks that `phones.adds()` gives what it gives on all 1,681 sayings.
* `v2_score.py` scores the rule on the first page's marks and counts its flags by name. `v2_marks.py` scores it on the second page's. `v2_time.py` judges the scratch chapters again and times it. `v2_sayings.py` hears takes as an options job does and compares with the check.
* `listen.py` and `listen2.py` cut the two pages, and `listen\template2.html` is the page. `listen\key.json` and `key2.json` map each clip to its take, and `answers.txt` and `answers2.txt` are AJ's marks.
* For phones of every name, copy the chapters' `take_check.json` files from containers. Since the take checks of 2026-11-13 each holds the `phone` block of every take, so nothing has to be heard again. A page still needs the takes' audio, from the app's mirror.
* `D:\fish.wt\scratch\n7` on the laptop has such a copy: the library without audio as containers had it on 2026-11-13 at 13:10, with the `phone` block of the 8,918 takes of its 40 checked chapters. From the repo, `judge_time.py` there times `takecheck.rejudged()` on it, profiles it with `--profile`, and writes what it derives for every take with `--dump`. `before.json` is that dump from before step N7. `vt_dump.py` does the same for `voice_text()`, and `vt_before.json` is its dump from before. `align_same.py` compares `takecheck.align()` with the alignment over every pair of words.
