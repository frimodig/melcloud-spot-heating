# Security

Please report a vulnerability privately, not in a public issue: use **Report a vulnerability** on the
repository's Security tab (GitHub private vulnerability reporting).

Say what you found, how to reproduce it and what it lets someone do. Reports are handled on a best-effort
basis by one maintainer. Only the latest `main` is supported.

## What matters most here

- **The phone page trusts a login header** (`[web] user_header`) set by Tailscale or another proxy. It must
  listen on `127.0.0.1` only: reachable any other way, anyone can send the header. See
  [hosting](docs/hosting.md).
- **`.env` holds the MELCloud password** and `web.env` holds the page's settings. The page's process is
  deliberately run without the MELCloud password. Keep both files `chmod 600` and out of git.
- **The controller uses MELCloud's unofficial API.** A problem in that API or in `pymelcloud` is theirs to
  fix, but tell me if this project uses it in a way that makes it worse.

Never put a password, token or ping URL in an issue. Read any log excerpt through for credentials and
personal data before you paste it.
