# Woow_odoo_document_management
WoowTech Document Management for Odoo 18 Community (LGPL-3, clean-room implementation)

The repository holds one Odoo addon, `woow_document_management` ("Woow Document Management",
zh_TW "WoowTech 文件管理"; app menu "Documents" / 「文件」). It depends on `mail` and `portal` only.

What it provides (design scope):

- folders with members and four permission levels (view, upload, edit, manage), inherited down the tree;
- documents stored as Odoo attachments with server-side upload validation, quota and audit log;
- a portal area under `/my/docs` for portal contacts;
- expiring, revocable share links under `/woow-docs/s` (the token lives only in the URL fragment);
- trash, legal hold, scheduled clean-up and an attachment overview for selected internal users.

> Status: implemented and under review before release. The checks and the gates that a release has
> to pass (static tools, the sandbox suite with its browser tour, coverage) are described in
> `dev/README.md`.

## Repository layout

| Path | Content |
|---|---|
| `woow_document_management/` | the addon |
| `tools/` | the 8 static security tools, their configuration and self-tests |
| `.security/sweep.yaml` | repository settings of the WoowTech security sweep (changes always go to a human) |
| `.github/` | CI workflow (static tools, semgrep, gitleaks), gitleaks config, PR template |
| `docs/CONTRACTS.md` | cross-lane interface contracts (public version) and their change log |
| `dev/` | sandbox and test helpers |

## Checks

```sh
sh tools/run_all.sh          # 8 static tools + self-tests + semgrep (if installed) + gitleaks
sh tools/run_all.sh --g1     # release gate mode
python3 -I -m unittest discover -s tools/tests -t tools/tests
```

Every tool is `python3 -I tools/check_<name>.py [--g1] [--root <repo>]`, prints
`<path>:<line> <RULE> <message>` and exits 0 (pass), 1 (violations) or 2 (tool error).

| Tool | Checks |
|---|---|
| `check_sudo.py` | the three allowed privilege escalations S1-S3, aliases, S2 shape, cron su checks |
| `check_routes.py` | route rules R1-R13 against `tools/routes.lock.json`; `--emit-entry-points` |
| `check_forbidden.py` | forbidden Python, QWeb and JS constructs |
| `check_sql.py` | raw SQL only at the statements of `tools/sql_allowlist.yaml`; advisory locks only in `services/locks.py` |
| `check_sweep_config.py` | `.security/sweep.yaml`, CI workflow and repository hygiene |
| `check_security_matrix.py` | ACL, record rules and groups against `tools/security_matrix.yaml` |
| `check_public_methods.py` | RPC-callable methods against `tools/public_methods.txt` |
| `check_cleanroom.py` | licence headers, legacy names, listed phrases, no vendored code |

Odoo tests run in a disposable sandbox database, never on a production site:
`odoo -d <db> -i woow_document_management --test-enable --test-tags /woow_document_management --stop-after-init`.

## Security and clean room

- Report vulnerabilities privately, see [SECURITY.md](SECURITY.md). Do not open public issues for them.
- How the code is written without third-party code: [CLEANROOM.md](CLEANROOM.md).

## Licence

LGPL-3.0 (see `LICENSE`, which supplements the GPL-3.0 text in `COPYING`).
Copyright 2026 WoowTech.
