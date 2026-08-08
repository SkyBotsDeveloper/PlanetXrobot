# Security Audit

Date: 2026-03-09
Scope: secret exposure hardening for subprocesses, logs, and user-controlled URL commands.

## Additional Hardening

6) Sensitive env isolation after startup
Files: `PLANETXROBOT/__init__.py`, `PLANETXROBOT/utils/security.py`
Patch: Sensitive env vars are stripped from the live process environment after config load so later child processes do not inherit bot secrets by default.

7) Clean subprocess environments
Files: `PLANETXROBOT/core/call.py`, `PLANETXROBOT/core/git.py`, `PLANETXROBOT/platforms/Youtube.py`, `PLANETXROBOT/plugins/sudo/restart.py`, `PLANETXROBOT/plugins/tools/kang.py`, `PLANETXROBOT/plugins/tools/tiny.py`, `PLANETXROBOT/plugins/tools/videoedit.py`, `PLANETXROBOT/utils/formatters.py`
Patch: Child processes now run with a reduced allowlisted environment instead of inheriting all loaded secrets.

8) Secret redaction in logs and error reports
Files: `PLANETXROBOT/logging.py`, `PLANETXROBOT/utils/errors.py`, `PLANETXROBOT/utils/security.py`
Patch: Known env and config secrets are redacted before being written to logs, paste output, or Telegram error messages.

9) SSRF and untrusted URL hardening
Files: `PLANETXROBOT/plugins/Kishu/webdl.py`, `PLANETXROBOT/plugins/misc/urlshortner.py`, `PLANETXROBOT/plugins/misc/downloadrepo.py`, `PLANETXROBOT/utils/security.py`
Patch: Public URL validation now blocks localhost, private IPs, credentialed URLs, redirects to untrusted hosts, and URLs containing secret values. `webdl` and `downloadrepo` were restricted to `SUDOERS`, and repo downloads are limited to HTTPS GitHub repos.

Date: 2026-02-05
Scope: command injection / shell execution and minimal safety hardening per request.

## Findings

1) Playlist command injection via yt-dlp shell invocation
File: `PLANETXROBOT/platforms/Youtube.py:239-266`
Issue: Playlist URL was interpolated into a shell command.
Patch: Replaced shell execution with list-args execution and added minimal URL character blocking (`; & | $ \n \r `).

2) Shell execution in ffmpeg speedup
File: `PLANETXROBOT/core/call.py:176-185`
Issue: ffmpeg command built as a shell string.
Patch: Use `create_subprocess_exec` with shlex-split args.

3) os.system use in update/restart commands
File: `PLANETXROBOT/plugins/sudo/restart.py:73-155`
Issue: git and heroku commands executed via shell (heroku push includes API key in command string).
Patch: Use `subprocess.run` with list args and suppress output.

4) os.system in video editing
File: `PLANETXROBOT/plugins/tools/videoedit.py:50-61`
Issue: ffmpeg command executed via shell.
Patch: Use `subprocess.run` with list args.

5) os.system in tiny sticker conversion
File: `PLANETXROBOT/plugins/tools/tiny.py:34-40`
Issue: lottie_convert commands executed via shell.
Patch: Use `subprocess.run` with list args.

## Secrets
`.env` is already gitignored and no `.env` file exists in the repo.

## Tests
`python -m py_compile PLANETXROBOT/platforms/Youtube.py`
`python -m py_compile PLANETXROBOT/core/call.py`
`python -m py_compile PLANETXROBOT/plugins/sudo/restart.py`
`python -m py_compile PLANETXROBOT/plugins/tools/videoedit.py PLANETXROBOT/plugins/tools/tiny.py`
