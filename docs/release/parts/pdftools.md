# PDF tools: split and merge (release 18.0.2.0.0)

## What users get

- **Split PDF.** On a PDF document (form header button, or the "Split PDF" entry of the action menu of the
  list or the form) a dialog asks how to cut the file: page ranges typed as `1-3, 4, 5-8`, or one document
  per page. Every part becomes a new document in the same folder, named `<original> (pages 1-3).pdf` or
  `<original> (page 4).pdf`, with the tags of the original and the splitting user as owner. The original is
  kept; a check box moves it to the trash when the user may write it (when the user may not, the original is
  kept and the notice says so).
- **Merge PDF files.** On two or more selected PDF documents ("Merge PDF files" in the action menu, or the
  form header button), a dialog lists the files in a list that the user orders by dragging the handle, asks
  for the folder of the new document and its name (`.pdf` is added when missing). The originals are kept; a
  check box moves to the trash the originals that the user may write.
- Both finish with a notice and open the new documents.

Rules the user meets:

- Page ranges must not overlap, must stay inside the file and run forward. Typing is tolerant of spaces,
  semicolons and full-width commas and dashes.
- At most 500 pages per source file, 100 documents per split, 50 files per merge, at least 2 files to merge
  (`PDF_MAX_PAGES`, `PDF_MAX_OUTPUTS`, `PDF_MAX_INPUTS` in `services/constants.py`). A merge whose sources
  add up to more than the internal upload limit is refused before any file is read. A merge takes at most
  2,500 pages in all (`MERGE_MAX_PAGES` in `services/pdf_tools.py`); the library copies pages in time that
  grows faster than their number (about 4 s for 2,500 pages, 12 s for 5,000), so the total is capped too. More
  than 50 selected files are refused with a message, not opened as an empty dialog.
- Password protected files, damaged files, files too large to process and anything that is not a PDF give
  a short message.

## Data model

No persistent model, no new field on an existing model, no new cron, no new route.

| Model | Kind | Purpose |
|---|---|---|
| `woow.document.pdf.split.wizard` | transient | `document_id`, `mode` (`ranges` / `pages`), `ranges`, `trash_original`, computed `can_trash` |
| `woow.document.pdf.merge.wizard` | transient | `line_ids`, `folder_id`, `name`, `trash_originals` |
| `woow.document.pdf.merge.line` | transient | `wizard_id`, `document_id`, `sequence` (the order of the merge) |
| `woow.document.pdf.common` | abstract | the steps both wizards share (checks, reading, storing, trash) |

`services/pdf_tools.py` is the only code that touches `odoo.tools.pdf`: `parse_ranges`, `single_pages`,
`open_pdf`, `page_range`, `write_pdf`. `open_pdf` first walks the page tree itself, visiting each node once
and stopping after `PAGE_TREE_NODES_MAX` (2,000) nodes, and refuses a node that is reached twice, so that a
small file cannot describe a tree with an enormous number of pages before the page limit is looked at. It raises `PdfToolError(code)`; the wizards turn it into a `UserError`.
No library is added.

## Public methods (all in `tools/public_methods.txt`)

- `woow.document::action_open_pdf_split_wizard`, `woow.document::action_open_pdf_merge_wizard`: first
  statement `has_group(...)` (document users), then `check_access('read')` on the selection; a hidden and a
  missing document give the same `AccessError`. They only return the dialog action with the selected ids in the
  context.
- `woow.document.pdf.split.wizard::action_split`, `woow.document.pdf.merge.wizard::action_merge`: first
  statement `check_access('write')` on the wizard row (an ordinary document user, the creator of the row).
- Overrides (`create`, `write`, `default_get`) of the wizards call `super()`.

## Security notes

- Rows of the three transient models belong to their creator (rules R-P1 to R-P3 in
  `security/pdftools_rules.xml`, listed in `tools/security_matrix.yaml`); no portal or public ACL.
- Everything is checked again when the button is pressed, in the caller's environment, never from what the
  dialog showed: the sources must be found by the caller's own search (record rules), live (active, folder not
  in the trash), files, and PDF; the destination folder must be visible, live and give at least the upload
  level (`_woow_require_rank(2)`). A source that does not exist and one the caller may not read give the same
  message. `create` of a wizard row or of a line refuses an id that the caller's search does not find, with the
  same message, so that the database never tells whether a hidden document exists. This covers the document of a split
  and of a line, the folder of a merge and the wizard of a line (a line cannot be put in another user's merge).
  The `default_*` keys of the caller's context are dropped on `create` of the three models, and a row that
  leaves such an id out is refused with the same message: a default cannot smuggle in an id that was not checked.
- Outputs are stored through `woow.document.create` of the caller (upload policy, quota, rights on the folder,
  the `upload` audit event). One quota measurement is shared by the whole run (`QuotaLedger`) and the files go
  in as several `create` calls, all in one savepoint: a refusal leaves nothing behind, not even a trashed
  original. A split makes its parts while they are stored: a call of `create` is made as soon as it holds
  `max_files_per_upload` files or as many bytes as the internal upload limit, so that the parts of a split
  (which can each carry a copy of a resource that the pages share) are never all in memory at once.
- The trash option calls `action_trash` only on the sources for which `_filtered_access('write')` holds.
- No `sudo()`, no raw SQL, no new route. No new kind of document, so the share-user rule of the release
  (hiding new types from portal users) has nothing to hide.
- Audit: `pdf_split` (document = source, detail `outputs`, `pages`, `trashed`, `output_ids`) and `pdf_merge`
  (document = the new file, detail `inputs`, `pages`, `trashed`, `source_ids`). Numbers and ids only.

## Administrator notes

- Upgrade with `-u woow_document_management`; nothing to migrate. The two actions appear for every document
  user after the upgrade; nothing to configure.
- The limits are constants (no setting). A merge is bounded by the internal upload limit setting (sources
  added up) and by `MERGE_MAX_PAGES`.
- Known limit: the library reads the whole file in memory, so a merge of many large files needs the memory of
  their total size at the same time.
