# Clean-room statement

`woow_document_management` is an original work of WoowTech, written to replace a third-party
document module without using any of its code. Copyright 2026 WoowTech, licensed LGPL-3.0.

## Inputs the implementers were allowed to use

1. WoowTech's own functional specification and design documents (written for this project; they
   describe behaviour, not code of any other module).
2. The source code of Odoo 18 Community core (LGPL-3), used as the framework reference.
3. Public Odoo documentation.

## What the implementers did not use

- The code, data files, UI text, translations, icons or screenshots of the replaced module or of
  any other document management module (community, enterprise or commercial).
- Customer databases or exports: tests and fixtures are synthetic.

## Rules applied to every change

- All UI labels, help texts, error messages, mail texts, translations and icons are written fresh.
- Every `.py`, `.xml`, `.js` and `.scss` file carries the "Copyright 2026 WoowTech" LGPL-3 header.
- `tools/check_cleanroom.py` rejects the technical names of the replaced module, a short list of
  phrases (`tools/cleanroom_forbidden.txt`), vendored `static/lib` code, symlinks and export
  attributes. The word "文件管理" (document management) is a generic term and allowed.
- Any fragment taken verbatim from Odoo core is recorded below with its source (LGPL attribution).
- Each pull request carries the clean-room declaration of `.github/pull_request_template.md`.
- An independent similarity comparison (G-SIM) is done by a separately authorised analyst before
  any public clean-room claim or app store listing; it reports scores only.

## Release 18.0.2: how the added features were specified

Release 18.0.2 adds features that users of document applications expect (one workspace screen,
favourites, locks, versions, shortcuts, requested files, roles and expiry dates for shared access,
a mailbox for a folder, automatic filing, PDF split and merge). For this release the work was
split in two roles, and this section states plainly what each role saw.

- **The analyst** looked at what is publicly visible of Odoo's own Documents application: its
  public user documentation and its public demonstration site, as a visitor sees them (screens
  and visible text only). No source code, page source, script, stylesheet or network content of
  that application was read. The screens were recorded with a capture tool restricted to what a
  visitor sees. From this the analyst wrote WoowTech's specification of the wanted behaviour in
  WoowTech's own words; the recorded screens are kept outside the implementers' workspace and
  outside this repository.
- **The implementers** received only that specification, this module's own code, the source of
  Odoo 18 Community core and public Odoo developer documentation. They did not see the recorded
  screens, nor any code, text, translation or icon of that application or of any other document
  management module. Everything in "What the implementers did not use" above holds for them
  unchanged.
- The analyst also coordinated the release: assigned the specification, merged the implementers'
  branches (resolving overlaps in shared lists) and reviewed the results. The analyst wrote no
  feature code from the observed material.
- All labels, messages, translations and icons of the added features are written fresh. Screen
  arrangements that are common to file managers in general (a folder tree beside a list of cards,
  a details panel, a preview) are used; no screen is a copy.
- Anonymous link sharing, spreadsheets, e-signature and automatic bill recognition are not part
  of this module.

## Fragments taken from Odoo core

None so far.

## Pull request declarations

| PR / branch | Lane | Declaration |
|---|---|---|
| lane/ci-sweep (day-0 skeleton, static tools, CI, sweep config, contracts) | ci-sweep | Written only from the design pack, Odoo 18 core source and public Odoo documentation; no other document module opened. |
