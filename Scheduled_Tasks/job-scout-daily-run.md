---
name: job-scout-daily-run
description: Runs the Job Scout local run in Job_Scout, which sends real mail and pushes the ledger, and checks the output by the rules in SCHEDULED_RUN.md
---

Read D:\dont_move\git_save\Job_Scout\SCHEDULED_RUN.md with the Read tool, never with a shell command, then follow the steps in that file exactly.

That file is the only source of rules for this task. Do not act from memory or guesswork, and do not add rules here.

If that file cannot be read, do not guess what to do, and do not start the local run. Instead run the line below **with the Bash tool** to send a toast, keeping the string exactly as written.

powershell.exe -NoProfile -File "D:\dont_move\git_save\Daily_Task\shared\notify.ps1" -Message "Job Scout 排程停擺 / 找不到 SCHEDULED_RUN.md / 需人工處理" -Title "Job Scout"

Then stop. This toast is the only notification channel.
