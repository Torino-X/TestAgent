# Security Policy

## Reporting a vulnerability

Please do **not** disclose suspected vulnerabilities, credentials, private
documents, or reproducible exploit details in a public issue.

Before this repository is made public, the maintainer must enable GitHub's
**private vulnerability reporting** feature. When it is available, report a
vulnerability through the repository's **Security** tab. Include the affected
version, impact, minimal reproduction steps, and any suggested mitigation.

If private reporting is temporarily unavailable, contact the repository owner
through GitHub without posting secrets or exploit details publicly.

## Scope

Please report issues involving authentication, authorization, secret handling,
user-uploaded files, document parsing, external-service configuration, and
dependency or container security.

## Configuration safety

Never commit real `.env` files, API keys, tokens, passwords, certificates, or
private documents. Use the committed `*.example` templates and supply runtime
secrets through your deployment platform.
