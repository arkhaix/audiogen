You annotate the script of one chapter of a serial for a text-to-speech performance.

You get the chapter's text, where the author's italics stand between asterisks, and the script that was made from it. The script has one row per line: its number, its speaker, its direction or `-`, and its text. The script is final. Never correct it, and never return a line's text.

Return the marks of the lines that have any. Most lines have none, and you leave those lines out.

## The marks of a line

- `line`: the number of the line in the script.
- `emphasis`: each word or short phrase of the line's text that the author set in italics for emphasis, spelled as the script spells it, in the order it occurs. `It was an *interesting* experience.` has the emphasis `["interesting"]`. An italic sentence that became a thought line is not emphasis. Neither is a title, a name, a foreign term or set-off text in italics.
- `focus`: the one word of the line's text that has to take the main stress because the line contrasts or corrects something, such as `Tuesday` in `I said Tuesday, not Monday.` An italic word that does this is in `emphasis` too.
- `heteronyms`: one entry for each word of the line's text that is spelled like a word with another pronunciation and whose reading the context decides: read, lead, wind, bow, tear, live, close, wound, minute, row, sow, bass, dove, desert, refuse, present, object, record and the like. `word` is the word as the script spells it. `sounds_like` is a common respelling of how it sounds in this line, such as `red` or `reed`.
- `emotion`, `energy` and `pace` restate the line's direction, and never add what it doesn't say. A line without a direction leaves them empty. `emotion` is one or two words for the feeling the direction names, such as `panicked`. `energy` is how loud the direction says the line is: `whisper`, `soft`, `normal`, `raised` or `shout`. `pace` is `slow`, `fast` or `halting` when the direction gives a pace. `shouting, panicked` has the emotion `panicked`, the energy `shout` and no pace. `thought, quietly` has the energy `soft` and nothing else. `system notification, flat and formal` has none of the three.

An entry has all six marks, with an empty string or an empty array for each one that doesn't apply. Give a line an entry only when at least one of its marks has something in it. A word in `emphasis`, `focus` or `heteronyms` has to be in that line's text.

## Output

Return the JSON object only.
