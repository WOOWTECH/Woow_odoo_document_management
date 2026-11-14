# Lane mailin: a mailbox for a folder

## What users get

A document administrator gives a folder an e-mail address (Documents, Configuration, Folder
mailboxes). Colleagues who are allowed to use it e-mail their files to that address; every
attachment becomes a document in the folder, owned by the sender, with the tags that the
administrator chose for the mailbox. The text of the mail is not kept.

- Allowed senders are the followers of the mailbox record. The administrator adds them in the chatter
  of the mailbox (the follower list), nothing else grants the right to send.
- At the moment the mail arrives the sender must also be an active internal user of the Document User
  group with upload level or more on the folder (a direct grant or through a group, or a document
  administrator). Lowering the right takes effect for the next mail. A folder in the trash accepts nothing.
- At most 10 attachments of one mail are stored (`MAIL_IN_MAX_FILES`); the others are ignored. Images
  that belong to the body of the mail are not files. A file that the upload policy or the quota refuses
  is skipped and the other files of the mail are still stored.
- A refused mail (the sender does not follow the mailbox, may not upload, is not an internal user, the
  mailbox is archived) stores nothing; the sender gets core's standard bounce message.
- Each filed mail leaves one short internal note on the mailbox ("Received by email: n stored, n refused,
  n ignored") and one audit row `mail_in` with counts only.

## Data model

`woow.document.mail.intake` ("Folder mailbox"), built on core's `mail.alias.mixin` and `mail.thread`:
`folder_id` (required, unique), `tag_ids`, `active`, `company_id` (stored, related to the folder), and the
alias fields of the mixin (`alias_name`, `alias_domain_id`, `alias_email`). The alias is created by the mixin
with contact policy "followers", routes to this model (`message_new`) and carries the id of its mailbox in
its defaults. The routing fields of the alias (model, contact policy, defaults, forced thread, parent) are
fixed: a write or create that names them is refused; only the name, the domain and the bounce text change.
The model is in `WOOW_MODELS`: attachments of its rows are never public and never carry a token.
A folder that is deleted for good (purge, uninstall) deletes its mailbox and the alias with it.

## Public methods

Overrides only: `message_new` (the mail gateway, superuser only; every other caller and every unknown
mailbox or sender is refused with one error), `message_update` (always refused: a reply to the note is not a
mail for the mailbox), `message_post` (an ordinary post for internal users; after `message_new` it writes the
counts note and nothing of the mail). No new public method, route, cron or wizard.

## Security notes

- Folder access is the single source of truth. Documents are created with `with_user(sender)` in
  `_woow_sender_documents` (S4 of `tools/check_sudo.py`, `docs/CONTRACTS.md` 4.3): the gateway is the
  superuser and `create` of a document skips every folder check for the superuser, so the one elevation of
  the lane goes the other way: it lowers the environment to the sender's own rights, and the folder upload
  level, the company rules, the tag fields, the upload policy and the quota are judged exactly as for an
  upload made in the interface. The same function decides at alias time (a bounce) and at storing time.
- Only the gateway reaches the mailbox: the route guard `_message_route_process` accepts a route to the
  mailbox only from the superuser (fetchmail) or a system administrator (the gateway script), and only
  when the first address of the raw From header is the address core resolved (as for documents and folders).
- Nothing of the mail stays on the mailbox thread: core posts the whole mail on the record that `message_new`
  returns, so `message_new` returns the mailbox with an in-process marker (an instance of `MailInNote`;
  a JSON context from a client can never be one) and `message_post` then writes only the counts note and
  returns an empty record, so that core does not add the recipients to it. The Message-Id header is kept on
  the note because core's own duplicate check reads it: a mail seen twice is filed once.
- Global rules R-I1 and R-I2 hide the alias row and the follower rows of a mailbox from internal users
  who are neither document administrators nor system administrators (core would let every internal user
  read both tables). Every other alias and follower row is untouched. A side effect: an ordinary user who
  calls `message_process` over RPC finds no alias, so no route.
- Mail is trusted as far as the mail server vouches for the sender: the From address is not authenticated
  by Odoo. A site should have its incoming mail server check SPF, DKIM and DMARC before it hands mail to Odoo.

## What an administrator must know to deploy it

Inbound mail needs, in general terms:

1. An alias domain (Settings, Technical, Alias Domains) whose DNS has an MX record pointing at the mail
   server that receives the mail, and a catchall address on that domain.
2. A way to hand received mail to Odoo: either an incoming mail server (IMAP or POP, Settings, Technical,
   Incoming Mail Servers) that fetchmail polls, or the mail gateway script run by the mail server for the
   addresses of the alias domain, logging in as a system administrator.
3. The mailbox (Documents, Configuration, Folder mailboxes): choose the folder, the alias name and the tags,
   then add the allowed senders as followers.
4. The sender's address in the mail must be the address of the sender's contact (the partner of the user).
   Mail from another address is treated as an unknown sender and bounced.

The upload limits, the allowed file types and the quota of the module apply to mail exactly as to other
uploads. Outgoing mail is needed only for bounce messages.
