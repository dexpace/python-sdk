# Security Policy

## Supported versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | :white_check_mark: |

## Reporting a vulnerability

Please **do not** open a public issue for security vulnerabilities.

Instead, report privately via
[GitHub private vulnerability reporting](https://github.com/dexpace/python-sdk/security/advisories/new)
(Security tab → "Report a vulnerability"). If you cannot use GitHub, email
[o.mazari.om63@gmail.com](mailto:o.mazari.om63@gmail.com) with
`[SECURITY]` in the subject line.

Include what you can of the following:

- The affected package(s) and version(s)
- A description of the vulnerability and its impact
- Steps or a proof of concept to reproduce it

You can expect an acknowledgement within a few days. Please allow time for
a fix to land and be released before disclosing publicly.

## Scope notes

- The SDK is a **toolkit**, not a service: it executes no network I/O of
  its own. Transport-level vulnerabilities (TLS, connection handling)
  usually belong to the underlying HTTP library (`httpx`, `aiohttp`,
  `requests`, or the standard library) — report those upstream.
- In scope here: credential handling (`http.auth`), header/URL redaction
  in logging, redirect safety (`Authorization` stripping, userinfo
  dropping), body capture, and challenge parsing.
