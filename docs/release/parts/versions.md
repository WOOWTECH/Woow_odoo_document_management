# Lane versions: keep earlier versions of a file

## What it adds for users

- A file keeps its history. On the document form there is a **New version** button (shown to people
  who may change the file) and a **Versions** tab. Uploading a new version makes the new file the
  current one and keeps the file that was current as the newest version (number 1, 2, 3, ...).
- The document keeps its name. When the new file has another extension, the name takes the new
  extension (`report.txt` becomes `report.csv`), so a name never disagrees with the type of its content.
- Every row of the Versions tab has **Download**, **Make current** and **Delete**. Make current keeps
  the current content as a new version and puts the chosen one back (the chosen row stays in the list).
  The latest 20 versions are kept; older ones are removed, oldest first.
- A document under legal hold keeps all its versions: its versions cannot be deleted, and a new
  version that would push the oldest one out is refused with a message.
- Download of a version: `GET /woow_document/version/<id>/download`. It answers a missing, a hidden
  and a forbidden version with the same page as the download of a document.
- ZIP downloads hold the current content only. Purging a document deletes its versions and their files.

## Data model

`woow.document.version` (one row per earlier content): `document_id` (required, cascade),
`number` (unique per document), `name` and `mimetype` as they were, `datas` (`Binary(attachment=True)`),
`file_size`, `checksum`, stored `company_id` (related to the document). `create_uid` and `create_date`
say who replaced the content and when. No chatter. The model is in `WOOW_MODELS`: its files are never
public and never carry a token. Extension of `woow.document`: `version_ids` (internal users only) and
`version_count`; both are server-only fields (an RPC write that names them is refused).

The stored bytes of versions count towards the quota without any change to the quota code: the usage
query already counts every stored file once per stored content, so a version that shares its file with
the current content (or with another version) costs nothing and a different content costs its size.

## Public methods

- `woow.document::action_new_version` (needs write access; opens the wizard)
- `woow.document.version.wizard::action_confirm` (the upload; the upload policy and the quota apply)
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
- No `sudo()`, no raw SQL. Version files are stored through the ORM field of the version; the new
  content passes `woow.document.upload.policy` and the quota exactly as a replaced file does.
- The audit log gets `version_add`, `version_restore` and `version_delete` (numbers and counts only) and
  `download` for a downloaded version (detail: its number).
- A new version is refused for a link, for a document in the trash and, when the lock feature is
  present, for a file locked by another user (`_woow_lock_holder`, empty until that feature exists).
- Concurrency: the new content is written first, so two uploads at once meet on the row of the file and
  the second one is run again; the unique `(document_id, number)` index is the second line.

## What an administrator must know

- Upgrade (`-u`) adds the table, the ACL rows, the rules and the views; nothing is migrated.
  Existing documents have no versions. Replacing a file by a direct write (the old way of the API)
  still works and does not keep a version: only the **New version** form does.
- The limit of 20 versions per document is `MAX_VERSIONS_PER_DOCUMENT` in `services/constants.py`.
- The route is registered in `tools/routes.lock.json` (11 entry points with the added route).
