# Release 18.0.2.0.0, part `sharing`: roles and expiry dates

## What users get

- **An end date for access.** A folder member and a person a document is shared with can both have
  an "Access ends" date. The last day of access is that date; the day after, a scheduled action
  (every 30 minutes) removes the row, so the person loses the access. The date cannot be set in the
  past. The member list of the folder form shows the column.
- **Sharing one document as viewer or editor.** The document form has a "Shared with" tab (an
  editable list: person, role, end date) and a "Share" button that opens the same list in a small
  dialog. A viewer reads the document. An editor can also rename it, replace its content, change its
  tags and description, and move it to the trash. An editor by share cannot move the document to
  another folder, give it to another owner, manage who it is shared with, or restore it from the
  trash. A portal contact with an active portal account can be shared with, as viewer only.
- **One choice for every document user.** The "Share" dialog of a folder has "All document users:
  no access / view / upload / edit". It creates, changes or removes the member row of the document
  user group, next to the member list.

## Data model

- `woow.document.folder.access.expiry_date` (Date, optional, not in the past).
- `woow.document.share` (new, in `WOOW_MODELS`): `document_id` (cascade), `subject_type` (`user` or
  `partner`), `user_id`, `partner_id`, `role` (`viewer` or `editor`; a portal contact is always a
  viewer), `expiry_date`, stored `folder_id` and `company_id` (both related to the document). One row
  per document and person (unique index), at most `SHARE_ROWS_PER_DOCUMENT_MAX` (200) rows per
  document.
- `woow.document`: `share_ids` (rows), `shared_editor_user_ids` (stored, server only, readable like
  the other lists), `woow_user_can_share` (computed, not stored).
- `woow.document.folder`: `woow_all_users_level` (computed with an inverse; `none`, `read`, `upload`,
  `edit`, and `manage` only as something to read).

The rows are the source of three stored lists: `shared_user_ids` (everyone with a row),
`shared_partner_ids` (every contact with a row) and `shared_editor_user_ids` (users whose role is
editor). Server code rebuilds the lists from all rows (not only the ones the caller reads) whenever a
row is created, changed or deleted. A direct change of `shared_user_ids` or `shared_partner_ids`
(still allowed as in 18.0.1) is mirrored into rows: new people become viewers without an end date,
people who left a list lose their row, people who stay keep role and end date. The mirror accepts any
user or contact that 18.0.1 accepted in those lists; rows made by hand must name a document user or a
contact with an active portal account.

## Public methods

- `woow.document::action_open_share_dialog` (needs write on the document and the manage level of its
  folder)
- `woow.document.folder::action_open_share_dialog` (needs the manage level of the folder)

`create`, `write` and `unlink` of `woow.document.share` are the other entry points; each starts with
its own access check.

## Security notes

- **Who may change rows.** Exactly who may change `shared_user_ids` / `shared_partner_ids` in 18.0.1:
  the caller can write the document and holds the manage level of its folder (the owner of the folder,
  members with manage, document administrators). The check runs before anything is read or locked and
  answers the same for a missing and for a hidden document. A share gives no management right.
- **Record rules.** R-D2 (document write) gets one more way in: a user in `shared_editor_user_ids`, when
  the document is active and its folder is not in the trash. Nothing else of `rules.xml` changes. New
  rules R-S0 to R-S3 (`security/sharing_rules.xml`): company, read (the rows of the documents the user
  reads, as R-D1 judges it, written out in full because a sub-search of a rule runs without rules), manage
  (folder manage level; also reads the rows of its folders, trashed documents included), administrator.
  Portal users have no ACL row on the model; the new fields have `groups='base.group_user'`.
- **Server only.** `shared_editor_user_ids` and the stored `folder_id` / `company_id` of a row cannot be
  written by a client. The marker of server work (`CTX_SHARE_SYNC`) is honoured only when its value is an
  in-process object; the same name sent by a client as a plain value is ignored.
- **An editor by share** keeps `folder_id` and `owner_id` as they are (a document that is only shared with
  the caller). A user who could already write the document through its folder (edit level, or owner with
  upload level) is not limited.
- **Audit.** A row change by a user is one `access_change` row per document with the ids of the rows.
  The cron writes one `access_expire` row per removed member or share (folder or document id, row id).
  No names, addresses or file names are logged.
- **Concurrency.** A change of rows first locks the documents it touches (row lock, id order, bounded
  wait), so two changes never rebuild the lists from stale rows.
- **Dates.** The end date is compared with the server date in UTC, by the check and by the cron.

## Deploying

- `-u woow_document_management`. The migration `migrations/18.0.2.0.0/post-sharing.py` creates a viewer
  row without an end date for every entry already in `shared_user_ids` and `shared_partner_ids`
  (idempotent; the lists do not change).
- A scheduled action "Documents: End expired access" (every 30 minutes) is installed. It removes at
  most `ACCESS_EXPIRY_BATCH` (500) expired member rows and 500 expired share rows per run and asks to be
  run again at once while rows are left.
- Removing access is not instant at midnight: it takes effect at the next run of the scheduled action.
