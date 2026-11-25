# Poortwachter

*Eerst Nederlands, dan spelen.* Steam stays closed until the day's Dutch study roster is done. The day resets at **02:00**.

## Install (Linux Mint)

```bash
unzip poortwachter.zip && cd poortwachter
sudo ./install.sh
```

Then open **Poortwachter** from the menu (or go to http://127.0.0.1:7623/).

- `--no-autostart` stops the app from opening at login.
- `--user NAME` watches a different account from the one that ran `sudo`.
- Running the installer again upgrades Poortwachter and keeps your data.

## First-time setup (about 10 minutes)

1. **Settings → Claude.** Paste your Anthropic API key. Textbook grading uses it.
2. **Settings → Anki.** It usually finds your collection on its own. Click **Show decks** to confirm.
3. **Settings → Boekenwurm**
   - Paste the project URL and publishable key. They're the same values as `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY`.
   - In the Supabase dashboard, go to **Authentication → URL Configuration → Redirect URLs** and add `http://127.0.0.1:7623/bw/callback`.
   - Click **Email me a sign-in link**, then open that link on this computer.
4. **Textbook**
   - Upload your PDF.
   - Set the page offset. If printed page 1 is page 15 of the PDF, the offset is 14.
   - Paste your exercise list, one exercise per line: `H5 Oefening 3; 84-85`.
5. **Library → Weekly plan.** For each weekday, set the minute budget (default 90 on weekdays, 45 at weekends), the core tasks that are always on, and the mystery % (how much of the random fill stays hidden until you start it). Add italki to the core tasks on your lesson days.
6. **Library.** Edit, add or turn off activities. **Pool weight** controls how often an activity is drawn at random: 0 means never.

## How a day works

- **Each day's roster.** It has three parts:
  - your core tasks,
  - random activities that fill the rest of the time budget (varied across categories, and avoiding repeats from yesterday),
  - up to **2** unfinished tasks carried over from yesterday.
- **Tomorrow tab.** You can edit tomorrow's roster: swap, remove, add, hide tasks as ❓ mystery tasks, or add a "Surprise me" task. **Today is locked in.** You can add to it but not remove anything. Changes to the weekly plan apply from tomorrow.
- **How each task is checked:**

| Task | Done when |
|---|---|
| Anki | You've logged X reviews since 02:00 (optionally in a specific deck, optionally with no due cards left) |
| Boekenwurm reading / cards | Your account shows X pages, paragraphs or minutes, or X cards reviewed |
| Timer | The time has passed **while the task panel was open**, plus a short Dutch summary if the task asks for one |
| Writing | X words that are recognisably Dutch |
| Speaking | An in-app recording of at least X seconds (kept in History) |
| Textbook | You submitted answers and Claude graded them (corrected answer, the rule, an extra example) |
| italki / events | Honour system: a short note and an optional lesson link |

Anki and Boekenwurm are also checked automatically every 3 minutes. Steam can open on its own even when the app isn't open.

## The lock

- **Background service.** It runs as root. While the roster is unfinished, it closes Steam, its web helper and any Steam or Proton game, and shows a notification and the study app.
- **Steam menu icon.** It goes through a gate that shows what's left instead of starting Steam.
- **Status.** `poortwachter status`
- **Emergency key.** `sudo poortwachter noodsleutel`
  - You type a commitment sentence and wait 90 seconds. Steam then stays open until 02:00.
  - It's logged in History, and carryover still applies.
- **Blocking more programs.** Edit `/etc/poortwachter.conf` (root only) and run `sudo systemctl restart poortwachter`.
- **Removing it.** `sudo ./uninstall.sh`. Your history and recordings stay in `/var/lib/poortwachter` unless you add `--purge`.

You're the admin on your own machine, so this is friction, not a prison. Stopping the service needs `sudo`, and that's deliberate.

## Notes

- **italki** has no public API for students, so lessons are honour-based.
- **Boekenwurm reading** only counts once a session has synced, so finish or leave the session before pressing Check. EPUBs have no printed pages: 4 paragraphs count as 1 page (you can change this in Settings).
- **Privacy.** Everything stays local in `/var/lib/poortwachter` (root-only). The only things sent out are:
  - the pages of an exercise you submit, plus your answers (to Claude),
  - read-only requests to your own Boekenwurm account.
- **Logs.** `journalctl -u poortwachter -f`
- **Tests.** `python3 -m unittest discover -s tests`
