# Oefenen: the textbook, one question at a time

Textbook tasks can work through an **exercise bank**: the exercises of your own textbook with the answers from its
answer key, imported into Poortwachter. The bank is built from your copy of the book, so it is private: it lives only
in Poortwachter's database on your computer and never in this repository (`.gitignore` refuses `*oefenen*.json`).

## How a textbook task works

- Questions come **one at a time, in the book's order**, from where you left off. Each exercise shows its
  instruction, word list, example, the textbook page (as an image) and links to the pages with the rules or the text.
- Answers the key can check (multiple choice, single words, fixed sentences) are graded **here, instantly**: case,
  extra spaces and end punctuation don't matter; a missing or wrong accent counts, with a hint. Exercises where the
  book says more answers are possible send a non-matching answer to Claude to judge.
- Open answers (your own sentences) and anything that isn't an exact match go to **Claude** (your own key) together
  with the key's answer or model answer, at the end of the exercise. Free writing gets feedback only and isn't scored.
- At the end of each exercise: the score, the percentage (70% is a pass) and every mistake with the right answer.

## The rules (the owner's choices)

| Rule | |
|---|---|
| Length | at least 20 minutes on weekdays (Settings → Study time; never less than 20) |
| Counts when | the timer reached the target, **and** at least one question was answered in the session, **and** the exercise that is open is finished |
| Nothing answered | if the timer reaches the target with no answers, the whole session is cancelled: the time starts again from zero |
| Closing the page | pauses the timer (heartbeat every 10 seconds) |
| Keep going | always allowed. Twice the target or more is marked **took long** (not policed). Doing at least one more exercise after the task was done and reaching twice the target earns **+1 hour** toward the next free day, once per task |
| Hardcore (setting) | an exercise counts only at 70% or more; below that it comes back later, and the task needs at least one passing exercise |
| Not possible | takes an exercise out of the book (it doesn't count, its answers are dropped); put it back in the Textbook tab |
| Didn't work | skips an exercise today only (audio or speech checking not working, for example) |
| italki | "Did this with my italki teacher" replaces the day's textbook task, naming the exercises or pages. The book stays where it was; the next session asks whether to continue after the italki work or where you left off |
| Focus | choose a chapter or exercise to work from (Textbook tab, inside a task, or for a coming day in Plan). The book's own progress is kept: skipped exercises stay open |

## Listening and speaking

- **Listening** exercises play the book's track (downloaded by you from the book's website and added per exercise in
  the Textbook tab or inside the task). Replays are fine: the time counts.
- **Speaking** exercises record your answer and turn it into text **on this computer** with Whisper
  (`sudo poortwachter-whisper-setup`, about 1 GB), then check it against the key's transcript or model answer.
  Without it, or if it fails, "Didn't work" skips to another exercise.

## Bank format

```json
{
  "format": "poortwachter-oefenen/1",
  "book": "…",
  "page_offset": -1,
  "units": [
    {
      "id": "h01-o5", "chapter": 1, "chapter_title": "…", "section": "1.2", "section_title": "…",
      "title": "Opdracht 5", "pages": [27, 28], "read": [[23, 25]],
      "mode": "auto | open | listening | speaking | partner | website",
      "skill": "vocabulary", "minutes": 8, "audio": false,
      "instruction": "…", "example": "…", "words": ["…"], "context": "…",
      "questions": [
        {"n": "1", "prompt": "… … …", "type": "choice", "options": ["…", "…"], "answer": ["…"]},
        {"n": "2", "prompt": "…", "type": "text", "answer": ["a / b"], "lenient": true, "note": "…"},
        {"n": "3", "prompt": "…", "type": "sentence", "answer": []},
        {"n": "4", "prompt": "…", "type": "open", "model": "a possible answer"},
        {"n": "5", "prompt": "…", "type": "speak", "answer": ["what should be said"]}
      ]
    }
  ]
}
```

- `pages` and `read` are **printed** page numbers; `page_offset` is PDF page − printed page.
- `answer` lists every accepted answer; `"a / b"` inside one entry also means either.
- `partner` (with a partner or teacher) and `website` exercises are listed but never served alone.
- Importing a newer bank keeps the progress of exercises whose `id` stays the same.
