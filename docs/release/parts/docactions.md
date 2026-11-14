# Lane docactions: lock, duplicate, rename

## What it adds for users

- **Lock / Unlock.** A user who works on a file can lock it. While another user holds the lock
  nobody else can change the document's name, file, link, folder, tags, description, type, owner,
  contact, properties or archived state, and nobody else can move, trash, restore or delete that one
  document. Favourites, chatter, activities and sharing keep working. The holder, a manager of the
  folder (manage level) and a document administrator can unlock. The form shows the buttons, a
  banner when someone else holds the lock, and the holder and date; the document list has a
  "Locked" column and the search has the filters "Locked" and "Locked by me".
- **Duplicate.** A button (and a method for lists) that copies files and links into the same
  folder. The copy is named "<name> (copy).<extension>" (the suffix is translated; links get the
  suffix at the end), is owned by the caller and keeps the tags and description. Shares, lock, legal
  hold, chatter and contact are not copied. At most 20 documents per call (fewer when the "files per upload" setting is lower).
- **Rename.** A small wizard for one document or one folder (button on both forms). It only
  performs a normal `write` as the user, so the existing rules apply (file name policy, folder
  name uniqueness, edit level, locks).

## Data model

- `woow.document.lock_uid` (many2one `res.users`, readonly, not copied, `ondelete='set null'`),
  `lock_date` (datetime, readonly, not copied), `is_locked` (computed, searchable) and
  `woow_locked_by_other` (computed for the current user, used by the form). All four are limited to
  internal users (`groups='base.group_user'`); portal users cannot read or search them.
- `woow.document.rename.wizard` (transient): `document_id`, `folder_id`, `name`. ACL: document users
  and administrators, no portal access. Declared server-field free (`_woow_own_default_fields`
  is empty); it inherits `woow.document.server.fields.mixin` like the other models.
- One new record rule, R-W7 (`security/docactions_rules.xml`): a rename wizard row is its creator's
  alone (`create_uid = user.id`, document users, all four operations), like R-W2..R-W6 of the
  other wizards. No new persistent model, no new route, no cron, no sudo, no raw SQL.

## Public methods (all in `tools/public_methods.txt`)

| Method | First check |
|---|---|
| `woow.document::action_lock` | `check_access('write')`, then per document: unavailable (trashed) and held-by-other refusals |
| `woow.document::action_unlock` | `check_access('read')`, then holder / folder manage / administrator per document, then write access to it |
| `woow.document::action_duplicate` | `check_access('read')`, internal users only, call size (20 and the files-per-upload setting), folder upload rank per source |
| `woow.document::action_open_rename_wizard` | `check_access('write')` |
| `woow.document.folder::action_open_rename_wizard` | edit rank on the folder |
| `woow.document.rename.wizard::action_rename` | `check_access('write')`, the target is searched under the caller's rules |

`action_trash` and `action_restore` of documents are overridden only to refuse while another user
holds a lock (then they call the existing method). `write` and `unlink` of `woow.document` are
extended the same way; `woow.document.restore.wizard.action_restore_to` is extended because it
moves a document without going through `write`.

## Security notes

- The lock fields are server-only: they are added to the guard lists of `models/document.py`
  (`_woow_own_default_fields`, and the "protected" sets of `create` and `write`). An RPC `create` or
  `write` that names any of them is refused, for every user except the superuser; a stored default or
  a `default_*` context key cannot set them. `action_lock` and `action_unlock` write them with the
  parent class `write` (the same way the trash actions write their server columns), after their own
  checks.
- A lock never gives access: locking needs write access, unlocking needs a right to the document
  and, in the end, write access to it (the ORM write); a lock never hides or reveals anything. The
  refusal message names neither the holder nor any hidden record.
- Portal users cannot read or search the lock fields, and cannot lock, unlock, duplicate or rename.
- Folder operations (trash, restore, move) are not stopped by locks of documents inside them. The
  deletion of a trashed folder removes the documents in it, locked or not: the folder purge marks
  its own call with an in-process object (a context value that is that very Python object, which
  an RPC context cannot carry); no other path switches the lock check off. The superuser and crons are
  never held back.
- Duplicate goes through the normal `create`: upload policy, quota, the file-count cap, owner
  checks and the `upload` audit row apply; a `duplicate` row (source id only) is added per copy.
  The whole call is one savepoint, so a refused file leaves no copy. The quota measurement is
  shared between the copies of one call (`QuotaLedger`), as in the attachment import.
- Audit: `lock`, `unlock` (`forced` is true when released by someone else) and `duplicate`; details
  hold ids and flags only.

## Import order (for the integrator)

`models/__init__.py` imports `docactions` right after `document` (before `trash_actions`) and
`docactions_trash` at the end. This matters: `test_trash_lifecycle` captures `type(documents).unlink`
and patches `unlink` on the trash class, which breaks (infinite recursion) when an `unlink` override
sits above the trash class in the model's method order. The lock checks of `write` and `unlink` are
therefore in the first file, and the overrides that must be asked before the trash actions
(`action_trash`, `action_restore`, the folder purge marker) are in the second.

## Deployment notes

- Upgrade is an in-place `-u`: two nullable columns are added to `woow_document`. Nothing to migrate;
  existing documents are unlocked.
- A user who is archived or whose rights changed can leave a lock behind. A manager of the folder or
  a document administrator releases it with Unlock. Deleting a user clears the locks he held.
- A holder who lost write access to the document cannot unlock it himself: the release is an
  ordinary write with no elevation, so he gets a short refusal ("This lock cannot be released from
  your account now. Ask a document administrator."). A folder manager releases it. The documents of
  a trashed folder are hidden by the read rule even from the folder's manager (they get the ordinary
  "no access" answer); a document administrator releases such a lock, or the folder is restored first.
- The limits (20 documents per duplicate call, 200 per lock or unlock call) are constants in
  `models/docactions.py`. A duplicate call is also held to the "files per upload" setting (1 to 20,
  default 10), whichever is smaller; the total size of the copies is bounded by the quota and the
  upload policy, as for every file.
- Translations of the new strings are in `dev/i18n/docactions.zh_TW.po`; the integrator merges them.
