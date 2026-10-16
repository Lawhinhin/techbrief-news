# Security

TechBrief News is a single-user local application. The HTTP server binds only to loopback, validates Host and Origin on changes, uses per-process CSRF tokens and limits request bodies. It has no public authentication layer. Do not expose it to the internet.

Tokens, recipient details, local archives and settings belong in `data/` or the environment. Do not put them in issue reports, screenshots, pull requests or Git history. A `.gitignore` is not a secret scanner: review every staged file before publishing. If a token leaks, revoke it with BotFather and create a replacement.

Windows credential storage uses DPAPI. Other platforms use plaintext, owner-readable credential files. Environment credentials are useful for service deployment, but the process and its environment must still be protected.

Default feed endpoints and article hosts are curated in `sources.json`. There is no browser API for arbitrary feed URLs. Changing that file changes the trusted network destinations. The collector blocks private IP literals, but does not implement a complete DNS-rebinding or arbitrary-URL proxy defense. Do not accept feed definitions from untrusted remote users.

Suspected vulnerabilities should be reported privately to the maintainer through an available private contact channel on the GitHub profile. If the repository enables GitHub private vulnerability reporting, use that feature. Do not post usable credentials, private data or exploit details in a public issue.
