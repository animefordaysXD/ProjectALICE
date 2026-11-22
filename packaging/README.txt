Alice 0.1.0 (Windows build)
===========================

Requirements
------------
- Windows 10/11, 64-bit.
- The OpenCode desktop app, installed and signed in to OpenCode Go.
  Alice asks OpenCode to run each model call; no API key is stored here.

Where your data lives
---------------------
Alice keeps its durable data OUTSIDE this folder, so replacing the app with a
new build never deletes your ledger or memory:

    %LOCALAPPDATA%\Alice\
      alice.db          event ledger
      logs\             application log
      raw\              redacted-source payloads
      memory\core.json  editable core memory (identity, rules, facts)
      memory\seed\      identity.md, rules.md, facts.md
      cache\            embedding model cache
      digests\          daily markdown digests
      reports\          weekly report markdown
      backups\          encrypted .alicebak files (keep 7 by default)
      opencode\         working folder for OpenCode calls
      paused.json       present only while model calls are paused

Double-click "Data folder.cmd" to open it.

Optional overrides:
    --data-dir "D:\AliceData"          one command only
    set ALICE_DATA_DIR=D:\AliceData    permanent for this shell
    set ALICE_PROFILE=work             separate profile under Alice\profiles

Backups are encrypted with Windows DPAPI and run nightly after consolidation.
Use "Backup now.cmd" or `alice.exe backup`; `alice.exe backup --list` shows
files, and `alice.exe backup --restore FILE --to DIR` verifies and restores.
A .alicebak can only be restored by the same Windows user. To reset, stop Alice
and delete the data folder.

First run
---------
1. Edit config\alice.yaml if needed. By default Alice watches:
     .\logs\*.log       (drop log files here, beside alice.exe)
     .\messages.txt     (one message per line, beside alice.exe)
2. Double-click "Init Alice.cmd" once. This creates the data folder, the
   memory\seed files, and memory\core.json.
3. Edit memory\core.json to change what Alice always remembers. Use
   "Data folder.cmd" to find it.
4. Double-click "Run Alice.cmd" to start. Ctrl+C stops it.
   The first run downloads the local embedding model (about 130 MB) into
   cache\; later runs reuse it.
5. Double-click "Alice GUI.cmd" for the read-only desktop dashboard.
6. Optional: double-click "Install startup.cmd" to run Alice hidden at logon.
   "Remove startup.cmd" unregisters it. Windows Task Scheduler is used because
   toasts need the signed-in user's session.

Commands
--------
  alice.exe status                 show schema, sources, counts, memory, spend
  alice.exe run --once             poll sources once, index memory, triage, exit
  alice.exe run --dry-run          same, using the fake model and no spend
  alice.exe approve                list pending memory proposals
  alice.exe approve <id>           approve a proposal (the only write path)
  alice.exe consolidate --day 2026-11-21   summarize a day and prune raw data
  alice.exe backup                 create an encrypted backup now
  alice.exe backup --list          list local backup files
  alice.exe backup --restore FILE --to DIR [--force]
                                   verify and restore a backup
  alice.exe report --days 14       weekly spend/token/activity report
  alice.exe report --write         write the report under reports\
  alice.exe startup --install      run hidden at logon (packaged build)
  alice.exe startup --status       check the logon task
  alice.exe startup --remove       remove the logon task
  alice.exe gui                    open the read-only desktop dashboard
  alice.exe pause --reason "..."   block every model call
  alice.exe resume                 allow model calls again

Notes
-----
- Alice is read-only and suggest-only. It never sends messages, edits files,
  or runs commands.
- Logs and messages are redacted before they are stored or could reach a model.
  Raw text stays in the data folder when redaction changed it.
- OpenCode Go is a subscription, so calls report $0. Token usage is still
  logged. Budget caps still apply to metered providers.
- Suggestions appear in a daily markdown digest under digests\. Toasts use
  win11toast; if Windows notifications are disabled, suggestions stay queued and
  the digest still works.
- The GUI is a local read-only dashboard. "Alice GUI.cmd" opens alice-gui.exe;
  it does not run sensors or reasoning in the background.
- Alice automatically moves an old `state` folder from this app directory into
  the per-user data folder on first start. If both already contain data, it
  leaves the old folder alone and logs a warning.
- A crash is safe: each event is committed before its cursor advances, so a
  restart loses nothing and dedupe prevents duplicates.
- Backups are local only. Alice never uploads the database, memory, or reports.
