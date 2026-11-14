# Release 18.0.2.0.0, part: automatic filing (lane `autofile`)

## What it adds for users

A document administrator decides that the files attached to a kind of record (for example projects)
are also filed as documents, in a chosen folder, with chosen tags and a chosen owner, from now on.

- Menu: Documents, Configuration, **Filing rules** (document administrators only). The form says in
  plain words that everyone who can open the target folder will see the filed files, even if they
  cannot open the record a file came from.
- The person who attaches a file needs no document right at all. The file stays where it was
  attached; the document is a second entry that shares the stored file (same checksum).
- Files of a chatter post are filed as well: a pending file is moved onto the record when the post is
  sent, and that move files it (the same hook sits on the write of `res_model` / `res_id`).
- When the original attachment is deleted, the filed document goes to the trash (a rule option, on by
  default). A document under legal hold stays.
- **File existing attachments** (button on the rule): looks at the next batch of attachments of the
  kind that are not filed yet and files them; shows what it looked at, filed and could not accept, and
  how many are left. It can be run again at any time and never files a file twice.

## Data model

- `woow.document.auto.rule` (in `WOOW_MODELS`): `name`, `model_id` (`ir.model`), `res_model` (stored
  related, server only), `folder_id` (required, restrict), `tag_ids`, `owner_id`, `trash_on_delete`
  (default on), `active`, `company_id` (stored related of the folder, server only), `backfill_cursor`
  (server only), `filed_count` (computed).
  - The kind: a stored model of another app. Transient and abstract models, `ir.*`, `mail.*` and this
    module's own models are refused (`_check_kind`).
  - The owner: an active internal document user; a document administrator, or a user who holds the
    upload level of the folder (`_woow_owner_acceptable`). It is judged again when a file is filed: an
    owner who stopped being acceptable is left off the document.
  - Tags: of no company or of the company of the folder.
- `woow.document`: `source_attachment_id` (set null with the attachment) and `auto_rule_id` (set
  null with the rule). Both are server only (listed in the server field lists, refused on `create` and
  `write` for everyone but the superuser) and readable by document administrators only.
- `ir.attachment`: one2many `woow_filed_document_ids` (document administrators only), used by the
  backfill to find what is not filed yet.
- `woow.document.autofile.wizard` (transient): the backfill of one rule.

## Behaviour that matters

- Hook on `ir.attachment.create` and on the write of `res_model` / `res_id`: an attachment is filed
  when it has a record (`res_id`), no `res_field`, type binary, is not public, is not empty and its kind
  has an active rule whose company is empty or the company of the attachment.
- It never breaks or slows an attachment: the kinds with rules (and whether any rule trashes) are kept
  in a registry cache that is cleared when a rule is created, changed or deleted, so an attachment of a
  kind without a rule costs no query; each filing runs in its own savepoint; any failure (policy,
  quota, a trashed folder, anything) is swallowed after the savepoint is rolled back and logged at
  info level with ids and the class of the error only. One call files at most
  `AUTO_FILE_BACKFILL_BATCH` files; what is beyond is left to the backfill.
- No recursion: a filing runs in an environment that carries an in-process marker (not a client
  context flag), and no hook runs inside it. The files of the documents have `res_field` and never
  match anyway.
- Upload policy and quota apply as for every document. The file size is compared with the limit
  before the content is read. The filing is credited for content that is already stored (it is the
  same stored file), as the attachment import wizard is.
- The backfill keeps a cursor on the rule (`backfill_cursor`): a run looks at the next batch after
  the cursor; a run that ends the list puts the cursor back at the start, so a file that an earlier run
  could not accept is tried again in the next pass. A document that was purged (deleted for good) is
  filed again by a later pass.
- Audit: `auto_file` per filed document (ids only: `{'rule': id}`), the usual `upload` and, on
  deletion, `trash` events, and `settings_change` (`{'rule': id}`) when a rule is created, changed or
  deleted.

## Public methods

- `woow.document.auto.rule.action_open_backfill()`: document administrators (`_require_manager`).
- `woow.document.autofile.wizard.action_file()`: document administrators (ACL, `check_access`, and
  `_require_manager`).

## Security notes

- The one elevation of the lane is `woow.document.auto.rule._woow_elevated` (`# sudo-ok: A1`, listed
  in `tools/check_sudo.py` and `docs/CONTRACTS.md` section 4.3). The rule made by a document
  administrator is the authorisation: the person who attaches the file is usually no member of the
  target folder. Everything that needs the elevation goes through this one method: creating the filed
  document, reading the attachments that the backfill files, finding the filed documents of a deleted
  attachment and moving them to the trash. Nothing else uses `sudo`, `with_user` or raw SQL.
- Access: only document administrators have an ACL on the rule and on its wizard (no row for users,
  portal or public); both carry the company rule (R-AF0) or the creator rule (R-AF1). A portal user
  gets nothing new: no ACL, no field.
- The marker of a filing and the marker of a source are in-process objects; a client cannot send them,
  so a client cannot switch filing off or set a source. A rule's `res_model`, `company_id` and
  `backfill_cursor` cannot be sent by a client either.
- Deleting the folder of a rule deletes the rule (through the ORM, so the cache follows). A rule of a
  company files only what was attached in that company.

## For the administrator

- Upgrade with `-u woow_document_management`. The new table and the new columns are created; there
  is no data migration. Nothing is filed until a rule exists.
- Choosing the kind of record in the form reads the model catalogue (`ir.model`), which Odoo opens to
  the technical "Access Rights" group only: a document administrator who has no such right can read and
  change the other fields of a rule but cannot pick another kind in the drop-down.
- A rule is not retroactive. Use **File existing attachments** for what was attached before.
- Cache: the registry cache of Odoo is cleared (and signalled to the other workers) at each change of a
  rule. Rules change rarely, so this is cheap.
