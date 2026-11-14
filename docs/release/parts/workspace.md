# Release 18.0.2.0.0, part: workspace

## What users get

- The Documents menu now opens one screen: the folder tree, sections and tags on the left; the files the
  user may see as cards (kanban) or rows (list) in the middle; an activity view; the existing document
  form on click. The screen shows exactly what the user's own search over documents returns.
- Sections (one choice): My files (I am the owner), Shared with me (I am in the document's direct share
  list), Recent (changed in the last 30 days, `RECENT_DAYS`), Starred (my favourites).
- Stars. Any user who can read a document can star it, with no write right. A star is private: nobody
  reads another user's stars, a document administrator included.
- Buttons that show without selecting anything: Upload (a wizard: choose a folder where you may upload,
  add files, optional tags), New link (the document form for a link), New folder (the folder form), Trash
  (the deleted files list).
- Filters: Files, Links, My uploads, With activities. Groupings: Folder, Owner, Type, Month.

## Data model

- `woow.document.favorite` (new, persistent): `document_id` (required, cascade), `user_id` (required,
  cascade, always the caller), stored `company_id` related to the document. One row per document and
  user (database unique constraint). In `WOOW_MODELS`, so its attachments (it has none) could never be
  public.
- `woow.document` gets: `favorite_ids` (one2many, internal users, used to search), `is_favorite`
  (computed for the caller, searchable with `=` and `!=` on `True`/`False`), `woow_section` (not stored,
  search only), `woow_icon` (not stored; the Font Awesome class of the kind of file).
- Transient models: `woow.document.upload.wizard` and `woow.document.upload.wizard.line`. The bytes of a
  file wait in a column of the line table (`attachment=False`), never in an attachment, and the lines are
  removed when the upload is confirmed (Odoo's transient vacuum removes the rest).

## Public methods

- `woow.document.action_toggle_favorite()`: `check_access('read')` first, one record, creates or removes
  the caller's own row through the normal ORM (ACL and record rule apply, no `sudo`).
- `woow.document.upload.wizard.action_upload()`: `check_access('write')` first (the wizard row is its
  creator's alone, rule R-W7).
- Override of `woow.document.search_panel_select_range` (calls `super()`): see below.

## Security notes

- Rules: R-V0 (company), R-V1 (a favourite is its user's alone, every operation), R-W7 and R-W8 (the
  upload wizard and its lines are their creator's alone). ACL: favourites read/create/delete for document
  users (no write: a star is made or removed, never edited); no ACL for portal or public users. Portal
  users cannot even search by `is_favorite`, `woow_section` or `favorite_ids` (field groups).
- A star is created only for a document the caller can read under the record rules; a missing, hidden or
  trashed document is refused with the same message.
- `woow_section` accepts `=` and `in` with the four known values; anything else raises `UserError`. The
  domain it returns is always combined with the record rules by the search.
- The upload wizard creates every document through `woow.document.create` (upload policy, quota, the
  files-per-upload setting, the audit event `upload` with channel `backend`). The folder must give the
  user upload level (checked when the wizard is confirmed). A refused file is reported by name and does
  not stop the others. A line refuses a file above the internal upload limit when it is stored, and the
  number of lines of one wizard is bounded by the same setting.
- The folder tree of the search panel is hierarchical. Core drops a folder from a hierarchical tree when a
  folder above it is not readable by the user (members of a sub-folder only, say), so that the folder
  could not be reached from the tree. The override re-attaches such a folder to the nearest folder above
  it that the user can see (or makes it a root), using only folders that the call itself returned under
  the caller's record rules. Names stay folded by the folder display name; no id of an unreadable folder
  is returned.

## Deployment

`-u woow_document_management`. Adds two tables for the transient wizards, the favourites table and the
new columns on nothing (the added document fields are not stored). The SQL constraint on `ir_attachment`
that keeps this module's attachments private is rebuilt with the new model in its list. No data
migration. The old documents action stays installed (the list and form are shared); only the menu entry
points to the new action.
