# Changelog

Releases are git tags (`v2.0.0`). `poortwachter-update` installs the newest one.

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
