# Lane versions: keep earlier versions of a file

## What it adds for users

- A file keeps its history. On the document form there is a **New version** button (shown to people
  who may change the file) and a **Versions** tab. Uploading a new version makes the new file the
  current one and keeps the file that was current as the newest version (number 1, 2, 3, ...).
- The document keeps its name. When the new file has another extension, the name takes the new
  extension (`report.txt` becomes `report.csv`), so a name never disagrees with the type of its content.
- Every row of the Versions tab has **Download**, **Make current** and **Delete**. Make current keeps
  the current content as a new version and puts the chosen one back (the chosen row stays in the list;
  at the limit the oldest other version is the one that is removed).
  The latest 20 versions are kept; older ones are removed, oldest first.
- A version number is never given twice: after the newest version is deleted the next upload takes the
  following number, not the freed one, so the audit log never holds two events for one number.
- On the document form the **File** field of a file that already has content is read-only: the file is
  replaced with **New version**, which keeps the earlier content. The field still takes the first file of
  an empty document. Writing `datas` over the API replaces the content as in release 18.0.1 and keeps no
  version (server flows such as the mailbox and the requests rely on it).
- A document under legal hold keeps all its versions: its versions cannot be deleted, and a new
  version that would push the oldest one out is refused with a message. Only a document administrator may
  read the hold, so only an administrator is told that a hold is the reason; other users get a neutral
  message ("These versions cannot be deleted." / "This document cannot take another version.").
- Versions of a file in the trash cannot be deleted (a new version and a restore are refused as well), and
  none of the three actions is open to a user while another user holds the lock of the file.
- A hold on a folder does not protect the versions: the hold of the document is the one that counts, as
  for the permanent deletion of a document.
- Download of a version: `GET /woow_document/version/<id>/download`. It answers a missing, a hidden
  and a forbidden version with the same page as the download of a document.
- ZIP downloads hold the current content only. Purging a document deletes its versions and their files.

## Data model

`woow.document.version` (one row per earlier content): `document_id` (required, cascade),
`number` (unique per document, never reused), `name` and `mimetype` as they were, `datas` (`Binary(attachment=True)`),
`file_size`, `checksum`, stored `company_id` (related to the document). `create_uid` and `create_date`
say who replaced the content and when. No chatter. The model is in `WOOW_MODELS`: its files are never
public and never carry a token. Extension of `woow.document`: `version_ids` (internal users only) and
`version_count` and `version_last_number` (the number the newest version took, the counter behind
"never reused"); all three are server-only fields (an RPC write that names them is refused).

The stored bytes of versions count towards the quota without any change to the quota code: the usage
query already counts every stored file once per stored content, so a version that shares its file with
the current content (or with another version) costs nothing and a different content costs its size.

## Public methods

- `woow.document::action_new_version` (needs write access; opens the wizard)
- `woow.document.version.wizard::action_confirm` (the upload; the upload policy and the quota apply).
  The wizard row itself is judged when it is written: the document must be one the caller may change
  (a missing, a hidden and a read-only document get the same answer) and the file must pass the upload
  policy, so nothing the policy would refuse is parked in the table of the wizard.
- `woow.document.version::action_download`, `action_restore`, `action_delete`

`create`, `write` and `unlink` of `woow.document.version` are refused for everyone except the server paths
of `models/versions.py` (an object that exists in the process only, kept under the context key
`woow_version_swap`; a context sent by a client cannot carry it) and the superuser.

## Security notes

- Access: ACL for the document groups (read, create, unlink; no write) and no ACL for portal or public.
  Record rules (`security/versions_rules.xml`, matrix rows R-V0 to R-V5): R-V1 spells out the read
  condition of the document (R-D1), R-V2 the write condition (R-D2) for the server paths that make
  or remove rows, R-V3 the document administrators, R-V0 the company, R-V5 the owner of the wizard row.
  R-V4 is a global rule: no row is removed while its document is under legal hold. Record rules run
  as superuser, so it can read the hold, a field that only administrators read.
- No `sudo()`. One registered raw statement (`# sql-ok: Q31`, `_woow_lock_document_rows`): a row lock
  `SELECT ... FOR NO KEY UPDATE` on the document rows, see Concurrency. Version files are stored through the ORM field of the version; the new
  content passes `woow.document.upload.policy` and the quota exactly as a replaced file does.
- The audit log gets `version_add`, `version_restore` and `version_delete` (numbers and counts only) and
  `download` for a downloaded version (detail: its number).
- A new version is refused for a link, for a document in the trash and, when the lock feature is
  present, for a file locked by another user (`_woow_lock_holder`, empty until that feature exists).
- Concurrency: a new version, a restore and a delete first lock the row of the document (Q31, no row
  version is written). Two uploads of one file take turns: the second waits, then fails with a
  serialization error, which the server runs again on a fresh snapshot, so it takes the next number.
  A legal hold, a trash or an upload committed after the snapshot of a delete makes it fail the same way,
  so a version that has just been put under hold is never removed. The unique `(document_id, number)`
  index is the second line. Tests: `TestVersionRaces` (real transactions, no test mode).
- Merge note for the integrator: the content is replaced with `super(WoowDocument, self).write`, which
  skips the `write` of every extension of `woow.document` that is loaded after `models/document.py`
  (a lock or rename guard, the placeholder logic of requested files, shortcuts, automatic filing, PDF
  tools). `_woow_version_ready` / `_woow_history_open` judge write access, the file type, the trash and
  the lock holder (`_woow_lock_holder`: the field `lock_uid` once the lock feature is merged; it was
  only tested with a patched method here). After the merge re-read each such `write` and add its
  condition to `_woow_history_open`, and test that a locked, held or placeholder document refuses a new
  version and a restore.

## What an administrator must know

- Upgrade (`-u`) adds the table, the ACL rows, the rules and the views; nothing is migrated.
  Existing documents have no versions. Replacing a file by a direct write (the old way of the API)
  still works and does not keep a version: only the **New version** form does.
- The limit of 20 versions per document is `MAX_VERSIONS_PER_DOCUMENT` in `services/constants.py`.
- The route is registered in `tools/routes.lock.json` (11 entry points with the added route).
- The raw statement is registered in `tools/sql_allowlist.yaml` as Q31.
