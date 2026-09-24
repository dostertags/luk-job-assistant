---
description: Iniciar sesión en Luk / Log in to Luk (a browser window opens; you type your own credentials)
---

Log the user in to Luk, following rule 6 of the `luk` skill.

1. Call `account_status` with `check: true`. If the session is valid, say so (with the name, if
   known) and stop.
2. Tell the user: a browser window is opening; they log in to Luk there with their own account
   (email, Google or LinkedIn), and it closes by itself once they are in. You never see or type
   their credentials.
3. Run `luk login` with the Bash tool and `run_in_background: true`, never in the foreground (the
   default 120 s timeout would kill the login). Never add `--paste-cookie`.
4. Wait for the background task to finish:
   - Exit 0: confirm with `account_status` (`check: true`).
   - "login already in progress": a login window is already open; ask the user to finish there.
   - Any other failure: relay its message and the fallbacks it prints (`--browser msedge` or
     `--browser chrome`; LinkedIn, or email and password). `luk login --paste-cookie` is for the
     user alone, in their own terminal.
5. Never operate, screenshot or read the login window, and never ask for a password, cookie,
   token or 2FA code. The user can also type `! luk login` themselves.
