# Release 18.0.2.0.0 part: the workspace screen in the browser (lane wsui)

## What it adds for users

The Documents screen (kanban and list of the workspace action) now works like a file manager.

- **Cards with a picture area.** An image file shows the image itself (through the preview route,
  loaded lazily); every other kind shows a large icon: PDF, text, sheet, archive, link, shortcut,
  requested file. Under it come the name, the folder, the tags, the owner and the activity mark, and
  small marks for starred, locked and "has earlier versions". The star on a card still works as a button.
- **Folders above the files.** The sub-folders of the folder chosen in the left panel (the top folders
  when none is chosen) are compact cards with the name and the number of files. A click chooses that
  folder in the left panel. A path line shows the chosen folder, each part is a way back ("All folders"
  is the first one). The left panel lists every folder the user may open, empty ones too, by its plain
  name, and keeps the chosen folder in sight.
- **Selection.** A click selects a card (or a row of the list); Ctrl or Cmd adds or removes one, Shift
  takes the range. The control panel then shows the number selected, **Open** (one item), **Download**,
  **Share** (one item that the user may share) and an **Actions** menu: rename, move, duplicate, star
  or remove the star, create a shortcut, new version, split PDF, merge PDF files, lock, unlock and
  move to trash. An entry that does not apply to the selection is not shown; the server judges every
  call again and its message is shown when it refuses.
- **Opening.** A double click, or Enter on a focused card, opens a file in the core file viewer (images,
  PDF files and plain text; previous and next go through the files shown). Another kind of file is
  downloaded. A link opens in a new tab (http, https or mailto only). A shortcut opens its target
  through the server method; a requested file opens its upload dialog.
- **Dropping files.** Files dragged from the computer onto the card area are uploaded one by one into
  the chosen folder, with a progress line and the server's refusal for each file that fails, then the
  screen reloads. Without a chosen folder, or without the upload level there, the drop is refused with
  a message. Folders cannot be dropped (they are reported).
- **Trash.** The left panel ends with a "Trash" entry that opens the existing trash action.
- **Dragging cards to a folder.** The selected cards can be dragged onto a folder card or a folder of the
  left panel; they are moved through the existing move wizard, with its checks.
- **Details panel.** When the details panel of the inspector lane is registered, an info button of the
  control panel shows it on the right (props `resModel`, `resId`, `onClose`, `onChanged`).

## Data model, routes and server methods

Nothing. The lane adds no model, no field, no route and no public method; it reads through the ORM
service and calls the existing server methods and routes of the other lanes
(`/woow_document/upload`, `/woow_document/doc/<id>/preview` and `/download`, `/woow_document/zip`).

## How it is built

- `views/backend/wsui_views.xml`: two inherited views that only set `js_class` (kanban
  `woow_workspace_kanban`, list `woow_workspace_list`), one that replaces the card and adds the fields the
  screen reads, one for the list, and one that makes the folder tree of the search panel list every
  readable folder (`expand`, trashed folders left out).
- `static/src/backend/wsui/`: `workspace_utils.js` (kinds, URLs, link check, the entries that apply to a
  selection, selection by click), `workspace.js` (the state and behaviour shared by the two views),
  `workspace_components.js/.xml` (selection bar, folder strip, upload status, details host),
  `workspace_kanban.js/.xml`, `workspace_list.js/.xml`, `workspace_search_panel.js/.xml`, `drop_zone.js`.
  Styles are in `static/src/scss/wsui.scss`.
- The details panel is looked up in the registry category `woow_document_components` under `inspector`
  and rendered only when it is there; the info button is hidden otherwise.

## Security notes

- The screen is a convenience and adds no right. Everything it shows comes from ORM reads as the user
  (the records, the folder cards `search_read` on `woow.document.folder`, the names of the folders, the
  upload level `woow_user_rank`); every button calls an existing server method that checks access itself,
  and the move by dragging goes through `woow.document.move.wizard.action_move`.
- No HTML is built from data: only `t-out` of plain text, no `markup`, no `innerHTML`, no `t-raw`. Every
  record value that goes into a URL passes `encodeURIComponent`. The link of a link document is opened
  only when it is an explicit http, https or mailto address without credentials, with `noopener`.
- Only this module's routes and core routes are called. The upload sends the CSRF token and one file per
  request, so the refusal of one file (policy, quota, size) does not stop the others.
- A portal or public user has no access to the screen's models and fields (the fields it reads are limited
  to internal users or to the record rules); `tests/test_wsui.py` pins this.

## What an administrator must know

- Nothing to configure. The whole lane is one data file (`views/backend/wsui_views.xml`) plus the files
  under `static/src/backend/wsui/`: the two records that only set `js_class` attach the browser classes, so
  taking the lane out leaves the standard kanban and list of the workspace lane as they were.
- The unit tests of the components run in the browser from `tests/test_wsui.py`
  (`@woow_document_management` suite of the web client test runner) and need the same headless browser as
  the other tours.
