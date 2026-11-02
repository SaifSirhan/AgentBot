---
name: log-report
description: Read a log file and produce a JSON summary counting total lines, ERROR lines, and WARN lines without modifying the source log.
---

# Log Report

Read `/workspace/app.log` and write `/workspace/report.json` with exactly three keys: `total`, `errors`, and `warnings`. Do not modify `app.log`.

## Steps

1. Read `/workspace/app.log` in full.
2. Count:
   - `total`: all lines in the file.
   - `errors`: lines containing `ERROR`.
   - `warnings`: lines containing `WARN`.
3. Write `/workspace/report.json` as a JSON object with exactly these keys:
   ```json
   {"total": <int>, "errors": <int>, "warnings": <int>}
   ```
4. Verify the file was written and the counts match the log contents.

## Notes

- Treat each non-empty line as one entry; count lines as they appear.
- Match `ERROR` and `WARN` as substrings of the log level field.
- Only write to `/workspace/report.json`; leave `/workspace/app.log` untouched.
