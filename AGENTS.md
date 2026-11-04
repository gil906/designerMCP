# Public repository safety

This repository is public. Before every commit and push:

- Review all staged files and outgoing history for personal information, secrets,
  credentials, internal infrastructure, private domains, and machine-specific paths.
- Redact or omit sensitive material; never print matched secret values in logs.
- Never publish local MCP configuration, environment files, private keys, account
  profiles, private denylist files, virtual environments, or session artifacts.
- Run `python3 scripts/check_publication.py --staged`. Install repository hooks
  with `git config core.hooksPath .githooks`; do not bypass failing hooks.
- Use a local, untracked `.git/info/publication-private.json` array for additional
  identifying strings. Never copy that private file into documentation or commits.
- Inspect screenshots and binary metadata manually for personal information.
  Automated text checks cannot guarantee that images or prose are anonymous.
- Preserve third-party copyright notices, licenses, and required attribution.
  Public reference authors are not the owner's private identity. Do not falsely
  claim upstream content is anonymous, original, or unrestricted.
- Keep published setup instructions portable. Use `/path/to/designerMCP` examples
  rather than a contributor's real filesystem or internal network addresses.

Fix findings before publication and rerun validation. Do not rewrite already
published history or bypass GitHub push protection without explicit authorization.
