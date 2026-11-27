# Changelog

Releases are git tags (`v2.0.0`). `poortwachter-update` installs the newest one.

## [4.1.0] - 2026-11-27

- **A day the computer stays off is paused**, like a free day: no hour earned, the hours toward your next free day aren't wiped, the streak and the weekday ramp pause, and nothing carries over from it. History shows it as ⏸. Settings → Study time → "A day the computer stays off" can count it as missed instead.

## [4.0.0] - 2026-11-26

**Oefenen: your textbook, one question at a time** (docs/OEFENEN.md).

- Import an **exercise bank** made from your own textbook (it stays on this computer). Textbook tasks then serve its exercises one question at a time, in the book's order, with the page, the word list and links to the rules pages.
- Answers are checked **against the answer key** right away (accents get a hint); open answers go to Claude with the key's model answer. After each exercise: score, percentage (70% passes) and every mistake.
- **At least 20 minutes on weekdays** (Settings → Study time, never less). It counts once the time is reached, something was answered and the open exercise is finished. Twenty minutes with no answers cancels the session. Keep going as long as you like: twice the time is marked "took long", and doing another exercise after the task was done to twice the time earns **+1 hour** toward a free day.
- **Hardcore mode**: only exercises at 70% or more count.
- **Not possible** takes an exercise out; **Didn't work** skips it for today.
- **italki** can replace the day's textbook task (choose the exercises or pages); the book stays where it was and the next session asks where to continue.
- **Focus** on any chapter or exercise (Textbook tab, in the task, or for a coming day in Plan) without losing your place.
- **Listening** exercises play the book's tracks (add them per exercise); **speaking** exercises are checked on this computer with Whisper (`sudo poortwachter-whisper-setup`).
- **New default plan** (applied if you still had the old default; otherwise Settings → Study time → Use the recommended plan): weekdays the textbook and Boekenwurm reading; Anki as the daily review except a set of **30 Boekenwurm flashcards** on Wednesday and Saturday; the Boekenwurm diary every other day.
- **Raise weekday study time** (Settings → Study time): +5 minutes for every completed weekday after 7 in a row, up to 120. Weekends stay at 45 and don't count.

## [3.2.1] - 2026-11-26

- Fix: connecting to Boekenwurm failed with "connect.json answered 403". Cloudflare, which hosts Boekenwurm, refuses Python's default user agent; Poortwachter now names itself.

## [3.2.0] - 2026-11-26

Needs Boekenwurm with migration 0015.

- **Connect to Boekenwurm in one click, nothing to type**: Settings → Boekenwurm → **Connect to Boekenwurm**, or in Boekenwurm → Account → Experimental → Connected apps → **Connect Poortwachter on this computer**. Your browser goes to Boekenwurm, you read what will be shared and click **Allow**, and it comes back to Poortwachter, connected. No code, no database address or key.
- How it stays safe (PKCE, the way desktop apps sign in): Poortwachter keeps a one-time secret and sends Boekenwurm only its fingerprint; the code that comes back only works together with that secret, and only for a request this computer started, and only for the Boekenwurm address set in Settings.
- Typing a pairing code is gone. A connection made the old way keeps working.

## [3.1.0] - 2026-11-26

Follows Boekenwurm's privacy hardening of Connected apps (migration 0014). Needs that migration.

- **Allow step**: after you type the code, Boekenwurm asks you to **Allow** Poortwachter; Settings shows "Almost there" until you do, and Boekenwurm tasks say they're waiting.
- **Today only**: Poortwachter asks Boekenwurm for one study day (today, or yesterday before 02:00) with your time zone and the 02:00 reset, never a time range or history. Paragraph targets are gone (pages or active minutes).
- Clear messages for a mistyped or expired code and for too many tries (wait 10 minutes). A connection that was disconnected or unused for 90 days unpairs Poortwachter with a message.
- **No more Google Calendar feed through Boekenwurm**: Boekenwurm no longer stores anything for apps. Your plan is still at http://127.0.0.1:7623/calendar.ics for calendar apps on this computer, and calendar import into sessions is unchanged.

## [3.0.0] - 2026-11-26

- **Pair with Boekenwurm**: a code from Boekenwurm → Account → Settings → Connected apps. Poortwachter then gets Boekenwurm's daily numbers (never text): pages, active reading minutes, cards reviewed, diary written, and which reading was at or above your level. Needs Boekenwurm with the Connected apps update (migration 0013).
- Boekenwurm reading counts **pages OR active minutes**, whichever comes first, and by default only reading at or above your level. Older activities are converted (their page target, or 20 minutes).
- New task: **Boekenwurm diary entry** (write on your phone; only the fact that you wrote is shared).
- **Diary in Poortwachter** with Boekenwurm's 20 prompts (and an English line), and **grammar hints from LanguageTool** on every writing task (Settings → Writing).
- **Anki: several decks** per activity, ticked from your deck list.
- **Calendars**: events from your calendar's private iCal address become sessions by keyword (italki, taalgroep…), following moves and cancellations; your plan is published through Boekenwurm at a secret address Google Calendar can subscribe to (and locally at /calendar.ics).
- **Its own window**: Poortwachter opens as an app (GTK/WebKit) with a menu and dock icon; outside links open in your browser.
- Textbook pages are shown as images, with page buttons and zoom, so the study view works everywhere.

## [2.1.0] - 2026-11-25

- Boekenwurm sign-in with the 6-digit code from the sign-in email, so no Supabase redirect-URL setup is needed. (The link still works if the redirect URL is added.)
- Clearer messages for a wrong email address, an expired code, and Supabase's email rate limit.

## [2.0.0] - 2026-11-25

- Install day is free: the lock starts at the next 02:00.
- Plan up to a week ahead in the new Plan tab (today stays fixed).
- Sessions: italki lessons and library group study, weekly or on a date. Their minutes count toward the day's budget; if they fill it, only one minimum task stays required.
- Daily limits per category: at most 45 min textbook and 45 min required reading by default.
- Free days earned like PTO: +1 h per completed day, 24 h = 1 day, a missed day wipes partial hours, cap 15 days rising 1 a year to 20. The emergency key spends a banked day first.
- Textbook sets: one day's work of several exercises (max 45 min), imported from a sets file; graded together.
- `poortwachter-update` installs new releases from GitHub.

## [1.0.0] - 2026-11-25

- First version: Steam watchdog, daily roster with 02:00 reset, weekly template with core tasks, random pool and mystery tasks, carryover (max 2).
- Checks: Anki reviews, Boekenwurm reading and flashcards, timers, Dutch writing, voice recordings, Claude-graded textbook exercises, honour tasks.
- Emergency key (`sudo poortwachter noodsleutel`).
