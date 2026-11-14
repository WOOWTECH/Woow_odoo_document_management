# Release 18.0.2.0.0 part: inspector

## What it adds for users

The details panel of a document: select a file and see what it is, and act on it, without leaving the
screen.

- The panel shows: the kind icon, the name, the size and the time of the last change; the folder (a click
  chooses that folder on the workspace screen); the owner, the contact and the company; the tags (added
  from a search and removed with a button, only for someone who may change the document); where the file
  came from (the kind and the name of the source record, with an "Open" button; nothing at all when the
  user may not read that record); the star and the lock with their buttons; the number of versions with
  a button that opens the list of them; who the document is shared with (names, roles, end dates) and the
  "Share" button for those who may manage shares; for a requested file, the person asked, the due date,
  the note and the "Upload the file" button.
- Under it the chatter of the record (messages, notes, activities) of core `mail`, with the warning that
  the form shows when the folder has portal members.
- Every change made in the panel is shown at once and the screen that hosts the panel is told, so that
  its list or cards reload that record. A message, a file or a follower added in the chatter does the
  same (the panel gives the chatter the parent-reload hooks of core `mail`), and after a change made by
  a button the chatter fetches its messages and activities again. A call that the server refuses shows
  the server's message and reads the record again, so that the buttons match the server.
- The tag list closes with Escape and when the field loses the focus; Enter adds the first match.
- The close button of the page returns to the screen that opened it; when the page is the first screen
  (a direct address) it opens the workspace instead.
- The panel is a component of the web client. It is registered in the registry category
  `woow_document_components` under the key `inspector` and takes `{ resModel, resId, onClose, onChanged }`
  (and an optional `onSelectFolder(folderId)`: without it a click on the folder opens the workspace with
  that folder chosen, through the `searchpanel_default_folder_id` context key). The workspace screen
  looks the component up there; where there is no such screen the form of a document has a "Details"
  button that shows the same panel on a page of its own (client action `woow_document_inspector`, xmlid
  `action_woow_document_inspector`, the record comes from the button or from the id in the address).

## Data model

Nothing is stored: no model, no field, no cron, no route, no ACL row. The JavaScript is in
`static/src/backend/inspector/` (`inspector.js`, `inspector.xml`, `inspector_page.js`,
`inspector_page.xml`) and `static/src/scss/inspector.scss`; the view and the client action in
`views/backend/inspector_views.xml`; the server read in `models/inspector.py`.

## Public methods (tools/public_methods.txt)

- `woow.document::woow_inspector_data`: the one read of the panel (see below).
- `woow.document::action_open_source`: opens the record the document came from, when the caller's own
  rules find it; otherwise one neutral message.
- `woow.document::action_open_inspector`: the "Details" button of the form (a client action).

All three start with the same check: the caller must be an internal user (`has_group('base.group_user')`),
then the document must be found, live or not, by the caller's own rules. A document that is missing and
one that the caller may not read answer with the same message (`This document is unavailable.`); a portal
user is refused before anything is read, with a message that does not depend on the document.

## Security notes

- The panel adds no right. `woow_inspector_data` reads in the environment of the caller (no `sudo`, no
  `with_user`, no raw SQL) and returns a fixed set of keys: values the document model already lets an
  internal reader read, plus a few flags (`can_write`, `can_lock`, `can_unlock`, `can_share`,
  `can_upload`) that are only hints for the buttons. Every button calls an existing server method that
  judges the access again (`action_toggle_favorite`, `action_lock`, `action_unlock`,
  `action_open_share_dialog`, `action_open_fulfil_wizard`, the ORM `write` of the tags).
- Names of other records (owner, contact, company, the person asked, the people a document is shared
  with, the user who holds the lock, the source record, the target of a shortcut) are given only when the
  caller's own search finds that record. A name that the rules hide is left out (or shown as
  "Restricted" where the row itself is visible, as for a share row). The folder is returned with the
  folded name the model already gives and a `selectable` flag; the panel makes it a link only when the
  caller may read the folder.
- The list of people a document is shared with is the list the share rules already let the caller read
  (`R-S1`, `R-S2`, `R-S3`), at most `SHARE_ROWS_PER_DOCUMENT_MAX` rows; tags at most 100.
- The source of a document is data: the model must exist in the registry and be neither abstract nor
  transient, and both the model and the record must be readable by the caller.
- JavaScript: text is only shown with `t-esc`; no `markup`, no `innerHTML`, no `t-raw`/`t-out`; the only
  routes called are core ones (`/web/dataset/call_button/...` through `rpc`, and the ORM service); the
  panel makes no URL out of record data. `tests/test_inspector.py` pins these rules.

## Tests

- `tests/test_inspector.py`: access rules and every returned value for a reader, an uploader, an editor,
  a folder manager, a document administrator, an outsider, a plain internal user and a portal user;
  hidden contacts, sources and share rows; locks; shortcuts; requested files; the neutral answers; the
  static rules for the JavaScript files.
- `static/tests/inspector/inspector.test.js`: web client unit tests of the component (hoot), run by
  `TestInspectorBrowser.test_unit_tests_of_the_panel` (tag filter `woow_inspector`).
- `static/tests/tours/backend/inspector_tour.js`: one browser tour on a real record (facts, star, lock,
  tag, a note in the chatter, the click on the folder).

## Deploy notes

Update the module (`-u woow_document_management`); nothing to migrate. The browser tests need the
staged browser of the test sandbox, like the existing tour. A deployment that does not load the
JavaScript of the module keeps the standard views, but the "Details" button of the form then ends in the
web client's generic error dialog (the client action is not registered); nothing else changes. An
exception thrown while the file loads is a different case: like any script of the backend bundle it
stops the loader, so the fragile imports are kept to a minimum (the chatter of core `mail` is the only
one from another module).

## Known limits

- The chatter's own activity buttons (mark done, upload) refresh the chatter but do not tell the host
  screen; only messages, attachments and followers do. A requested file filled through such a button shows
  its new state in the panel after the next reload of the record.
- "Show versions" opens the list of the versions (with its download, make current and delete buttons)
  instead of the "Versions" tab of the form: Odoo 18 has no way to open a form on a chosen tab.
