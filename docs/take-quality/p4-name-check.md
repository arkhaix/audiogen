# P4, continued: refining the name check

This is an addendum to `phase-2.md`, package P4. The name check is live in its second version, and it is right when it flags and misses part of what AJ hears. This file says what is known, what is open, and the order to work in. Start it with "continue the name check in docs/take-quality". `DECISIONS.md`, "The take check flags a name when both recognizers hear a sound its spelling doesn't have", has the rule and why the first version went.

## Where the check stands

As of 2026-11-13:

* The rule is `phones.adds()`. A take is flagged `name` when Parakeet's spelling of a name and ZIPA's phones in its place both have a sound that the say-as spelling doesn't have. It reads skeletons, the consonants in order with one mark for each run of vowels, so it hears an added syllable or consonant and nothing else.
* It judges only a name that has a say-as. On containers that is Eole as "Eh-ole", Vouivre as "Vweevra", Louis as "Lewis", and Lauriane and its second spelling Laurianne as "Laurie Anne".
* AJ has marked 95 clips on two listening pages, and could judge 94. The rule agrees with him on 83. Of the 25 it flagged on the second page, 23 sound wrong to him. Of the 29 it passed there, 8 sound wrong.
* By kind of flag, on the second page: a vowel after the name 11 of 11 wrong to him, an h inside the name 4 of 4, a sound before the name 2 of 3, and other added sounds 6 of 7.
* In the library, after a take check of all 40 chapters that have names, 140 of the 345 takes that say Eole add a sound, 6 of 157 for Vouivre, and none of 140 for Louis and 186 for Lauriane. The takes of Vouivre are still those of the earlier spelling "Vwee-vruh".
* No render has run with the rule yet. At four takes in ten, Eole will be over the breaker in most chapters, so a render will report the name and take none of its takes again until AJ picks a spelling that screens clean.
* The option screen counts with the same rule. No option has been screened with it yet.

## What is not known

* What a retake for a name puts in the take's place. Nobody has listened to one.
* Whether a spelling that screens seven or eight of eight comes out clean in a book's chapters. The first version's screen gave "Eh-ole" 8 of 8.
* Whether "Vweevra" ends the fault AJ hears in Vouivre, which is "vroo" or "vroh" for "vruh".
* Whether the two breeze nodes give the same verdict on the same take. The first version measured that the nodes decode a few phones differently, and nothing has compared the skeletons.
* How the rule does on a name it was not shaped on. Almost every flag so far is on Eole.

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

* The marks are the only labelled data for this, and they live in the scratch copy alone, which is deleted when phase 3 is deployed. Put them in the repo: one record for each marked saying with the name's words, Parakeet's spelling, the phones near the span, the words on either side, and AJ's mark.
* Add a test that scores `phones.adds()` on them and pins the count, 83 of 94 today. A change to the rule then shows its effect on every mark in the test's diff.
* It is done when the test runs from the repo with no scratch folder.

### N1: watch the first live use

* This step waits for AJ. He screens Eole's options on the Names tab, picks a spelling and queues the renders the tab offers.
* Record what the screen gives each option, how long the job takes, and anything that fails. A screen of seven options is 56 takes.
* After the first renders, record for each chapter how many takes were flagged `name`, how many were taken again, how many of the new takes came out clean, and where the breaker held a name.
* Compare the picked spelling's screen count with its share of clean takes in the rendered chapters. That is the first evidence on whether a screen of eight predicts a book.
* Have both nodes hear the same 100 takes and compare the verdicts.
* It is done when these numbers are in "Where things stand" and any fault they show has a fix or a step here.

### N2: a third listening page

* Cut it after N1 from three groups: takes that a render kept after a retake for `name`, takes of the new Eole spelling that the rule passes, and takes of "Vweevra".
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
* Do this only if N2 shows the fault in takes of "Vweevra", or in another name.

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

* Judging 9,971 saved takes again took 69 s on the laptop, about 7 ms a take, with the old rule and the new one alike. Profile it first. Most of it is likely the token map that `voice_text()` builds again for every line.
* The Names tab counts only chapters whose check is current, because it can't judge on a request. With a fast judge it could, and a bump of `JUDGE_V` would show at once.
* This step has no listening page and can run at any time.

## Not planned

* A sound the take drops. The rule ignores it because both recognizers drop sounds, and AJ accepted "ay-oh" without its l.
* A forced alignment of the line's phones, which would place a name better than Parakeet's word times do. It needs a pronouncing dictionary that neither audio Python has.
* A better English reading in `phones.spelled()` for its own sake. Fix a reading when a false flag shows it wrong, and add the case to the test.

## Open ends from the build

* The Codex review of the second version did not run, because the account was at its usage limit. The Opus blind review found no blocking defect. Run the codex-review skill on the change when it is committed.
* The breaker needs four flagged takes in a chapter. A say-as that the rule reads wrong would have its takes taken again at every render in a chapter with fewer. No say-as in the library is read wrong today.
* A take made with an earlier say-as is judged against the new one until its chapter is rendered. The Names tab's count for a name therefore describes the old takes until then.

## What AJ does

* He screens Eole's options and picks a spelling, and queues the renders.
* He marks a listening page when a step asks for one.
* He decides whether a name over the breaker should be taken again anyway. With "Eh-ole" a retake would fix about one take in two.
* He decides whether names without a say-as should be judged at all, once N5 has its numbers.

## Data and tools

* `D:\fish.wt\scratch\p4` on the laptop has the scratch copy: the library as containers had it on the morning of 2026-11-13, the takes under `media`, and ZIPA's phones for the 826 takes that say Eole, Vouivre, Louis or Lauriane in `phones.jsonl`. Chapter 22 there was rendered again in scratch, so leave it out.
* `spans.py` writes one record for each name in each take, with the words on either side, into `spans_all.jsonl`. `skel.py` is the prototype of the rule, and `v2_same.py` checks that `phones.adds()` gives what it gives on all 1,681 sayings.
* `v2_score.py` scores the rule on the first page's marks and counts its flags by name. `v2_marks.py` scores it on the second page's. `v2_time.py` judges the scratch chapters again and times it. `v2_sayings.py` hears takes as an options job does and compares with the check.
* `listen.py` and `listen2.py` cut the two pages, and `listen\template2.html` is the page. `listen\key.json` and `key2.json` map each clip to its take, and `answers.txt` and `answers2.txt` are AJ's marks.
* For phones of every name, copy the chapters' `take_check.json` files from containers. Since the take checks of 2026-11-13 each holds the `phone` block of every take, so nothing has to be heard again. A page still needs the takes' audio, from the app's mirror.
