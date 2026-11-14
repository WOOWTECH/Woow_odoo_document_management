# Release 18.0.2.0.0 part: shortcuts

## What it adds for users

A document user can put a pointer to a file or a link into another folder without copying it.

- On a file or link, the button "Create shortcut" opens a small wizard: choose the destination
  folder, confirm. The shortcut gets the name of the target at that moment (the name is never
  synchronised afterwards) and belongs to the person who made it.
- A shortcut has its own row in the folder: kind "Shortcut", the "Open target" button (list and form),
  and it can be renamed, moved, trashed, restored and deleted permanently on its own.
- When the person looking at a shortcut may not read the target (no access, in the trash, or in a
  trashed folder) the row is greyed out, the form shows one notice, and "Open target" answers with one
  neutral message. The three cases look the same.
- A shortcut holds no content: it has no size and does not count against the storage quota. Download,
  preview and the ZIP downloads skip it (the ZIP never holds the target either).
- Shortcuts point to files and links only: not to a shortcut, not to a folder, not to a requested file.
  A shortcut cannot be duplicated or versioned.
- Portal and other share users never see shortcuts.

## Data model

- `woow.document.document_type` gets the value `shortcut` (removing the module's selection value
  deletes such rows).
- `woow.document.shortcut_target_id` (many2one to `woow.document`, indexed, `ondelete='cascade'`):
  set once, when the shortcut is created, by the server. A table check keeps "a shortcut has a
  target, every other document has none"; a Python constraint keeps the target a file or a link.
  Purging the target removes its shortcuts (database cascade).
- `woow.document.shortcut_target_available` (boolean, computed for the current user, not stored).
- `woow.document.shortcut.wizard` (transient): `document_id` (the target), `folder_id`.

## Public methods (tools/public_methods.txt)

- `woow.document::action_open_shortcut_wizard` (first statement `check_access('read')`).
- `woow.document::action_open_shortcut_target` (first statement `check_access('read')`; opens the target
  only if the caller's own search finds it live).
- `woow.document.shortcut.wizard::action_create`.
- Override `woow.document::web_read` (formats the target of a shortcut from the masked value).

## Security notes

- Rights to create: the caller must be an internal user with the upload level on the destination folder
  (and read access to it) and must find the target with his own search (record rules), live, of the same
  company as the destination. The wizard judges this again when it runs. The shortcut itself is created
  only with an in-process marker (`SHORTCUT_CTX_KEY` holding a module-level object): a plain `create`
  of `document_type='shortcut'` over RPC is refused, and so is any `create` or `write` that names
  `shortcut_target_id` (superuser code aside).
- A shortcut never gives access. The target is read in the caller's own environment only. The value of
  `shortcut_target_id` is formatted as empty in `_read_format` (so `read`, `search_read`, `web_read`,
  `web_search_read` and every view) and in `web_read` unless the caller's search finds the target.
  Searching, ordering, grouping, aggregating or exporting by the target is refused for everyone who is
  not a document administrator (an administrator reads every document anyway), so a search cannot be used
  to guess which document a shortcut points to.
- Record rule `R-S1` (global, `security/shortcuts_rules.xml`): share users get `document_type != 'shortcut'`.
  `R-W7`: a shortcut wizard row belongs to its creator.
- Every content path already answers "no file" for a document that is not of type `binary`
  (`_document`, `serve`, `_record_to_stream`, the ZIP loop, the quota that sums attachments); the tests
  pin this for shortcuts. The audit event of a created shortcut is `upload` with detail
  `{'bytes': 0, 'shortcut': 1}`; trash, restore and purge are logged as for any document.
- The `create` of the extension handles a shortcut by itself (it does not call the file code of the base
  `create`, which has nothing to check for a document without content) and then calls the base class
  `create` that stores the row. It replicates the folder, rank and owner checks of the base `create`.

## For an administrator

`-u woow_document_management` adds one column, one index and one table check; existing rows keep a null
target. No setting, no cron, no route. Nothing has to be configured.

## Integration notes (for the integrator)

- The duplicate action of the lane that adds duplication and the version actions of the lane that adds
  versions must refuse a shortcut. `copy` of a shortcut is refused here (`UserError`), which covers a
  duplication that calls `copy`; a version action has to check `document_type == 'binary'` itself.
- The lane that adds requests must keep its placeholder type out of the targets: the target check is a
  positive list (`binary`, `url`), so a new type is refused by default.
