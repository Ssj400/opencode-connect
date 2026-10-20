# Publication and stable-release checklist

## Release decisions

- [x] Owner and public repository: https://github.com/Ssj400/opencode-connect.
- [x] Owner-approved MIT license, included as `LICENSE` in exports.
- [ ] Define a private security reporting channel.
- [x] Owner authorization to publish the generalized experimental edition.

## Export and local testing

- [x] Export only `deploy.FILES`, never the complete personal installation.
- [x] Run `python3 -B -m unittest discover -v` and `python3 -B audit_public.py`.
- [x] Manually review exported files in addition to the heuristic audit.
- [x] Review the exact Git contents: no operational configuration, state, logs,
      backups, runtime artifacts, private directory links, or personal history.
- [x] Test an isolated export outside the operational installation (public test suite).
- [x] Check relative skill links and use the created repository's actual URL.
- [x] Verify that the export does not contain an operational `routes.json`.
- [x] Use English throughout documentation, instructions, comments, and runtime messages.
- [x] Separate the delivery core, OpenCode adapter, and command transports with isolated tests.

## Future integration — NOT verified in this edition

Requires explicit authorization, isolated infrastructure, and a model budget.
Do not run against working sessions or operational mailboxes.

- [ ] Record exact Python, OpenCode V2, Windows/WSL, and OpenSSH versions.
- [ ] Verify SSH, local API authentication, and the MCP handshake on both machines.
- [ ] Test queue delivery in both directions and the caller's session metadata.
- [ ] Test steer during generation; distinguish it from interrupting blocked tools.
- [ ] Transfer harmless files in both directions using fully isolated mailboxes.
- [ ] Check duplicates, mid-file/mid-POST disconnections, and relay restarts.
- [ ] Check deployment, rejection of others' changes, and recovery after partial failure.
- [ ] Check `create-session` with ask permissions without starting tasks.
- [ ] Do not extrapolate these results to untested versions or transports.

## Next platform milestones

- [ ] Linux ↔ Linux using a native Linux adapter.
- [ ] Windows ↔ Windows, distinguishing native Windows from WSL.
- [ ] macOS after the Linux and Windows milestones.

These are priorities, not supported configurations in the current release.

This checklist does not independently authorize Git operations. The owner explicitly
requested the initial experimental publication; that does not resolve outstanding
security or integration work.
