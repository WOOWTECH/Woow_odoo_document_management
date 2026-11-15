# Security policy

## Reporting a vulnerability

- Use GitHub **private vulnerability reporting** on this repository
  ("Security" tab, "Report a vulnerability").
- **Do not open a public issue or pull request** for a suspected vulnerability, and do not post
  details in discussions or commit messages.
- High and critical issues are fixed in a GitHub Security Advisory private fork and published
  with the advisory; medium and low issues are fixed in normal pull requests with neutral titles.
- Ordinary bug reports and feature requests are welcome as public issues.

## Supported versions

| Version | Odoo | Supported |
|---|---|---|
| 18.0.2.x | 18.0 Community | yes |
| 18.0.1.x | 18.0 Community | upgrade to 18.0.2 |

## Deliberate security design decisions (HD)

These are decisions we made on purpose; reports that only restate them are not vulnerabilities.
The full wording is supplied by the lanes that own each area (HD-01, 02, 03, 04, 07, 09, 10, 12, 13:
core models and access; HD-05: trash; HD-14 and HD-15: inbound email and portal text; HD-16 to HD-22: the features of release 18.0.2). HD-06, HD-08 and HD-11 are retired: they described link
sharing, which is not part of this module. Retired numbers are not reused.

- **HD-01** Native attachment download routes follow the same record rules but are not audited.
  *(text pending)*
- **HD-02** Internal readers of a folder can see its members and effective access. *(text pending)*
- **HD-03** Readers of a folder can see the numeric ids of its ancestors. *(text pending)*
- **HD-04** Chatter attachments on documents and folders follow Odoo's native upload rules, with
  one exception: they are never public and never carry or accept an access token, in any
  environment, so no link opens them without a sign-in (HD-12 is the same rule for what is served).
  The rule is kept by the module when a file is made or changed, and the database refuses to store
  such a file as well (a CHECK constraint on the attachment table, built from the list of the
  module's models), so a path that the module does not know fails with an error instead of leaving
  a file that a visitor can fetch.
  A file that is too large to be mailed is still taken out of the mail and replaced by a download
  link, but the link has no token. It opens only for a signed-in internal user who can read the
  record. Everybody else gets a not-found answer (HTTP 404) and no file: visitors without a
  session, and portal users as well. Portal members are a mail audience (HD-10), so they receive
  the mail and the link, but neither the file nor a working link. *(text pending)*
- **HD-05** The "restricted subfolder" flag is visible so that subtrees cannot be trashed blindly.
  *(text pending)*
- **HD-06** Retired.
- **HD-07** The portal folder write right is only an upload probe; real writes are refused.
  *(text pending)*
- **HD-08** Retired.
- **HD-09** System administrators can obtain superuser power; this is outside the module's scope.
  *(text pending)*
- **HD-10** Messages sent with "Send message" on a folder with portal members are mailed to every
  follower who can still read the folder or document, and to contacts without an account that
  staff added as followers; a reminder is shown above the chatter. Followers whose account lost
  access are no longer notified, unless a message names them as recipients. A portal contact who
  receives such a mail cannot answer it into the chatter: see HD-14. *(text pending)*
- **HD-11** Retired.
- **HD-12** Chatter attachments keep Odoo's native content-type rules; every streamed response
  carries a `default-src 'none'` content security policy. A chatter attachment is never served
  on the strength of an access token, whoever stored the token, and never because it is public:
  the public flag is kept off these files too, and since no such row can be stored (HD-04: the
  database refuses it), a public file of a document or folder does not exist to be served. The two
  decisions are one rule: no link opens a chatter attachment without a sign-in. *(text pending)*
- **HD-13** A folder that does not inherit its access can only narrow what its parent grants, and its
  owner is held to that as a member is. The owner has the manage level of the folder, so a user who is
  not a Document Administrator can make someone its owner only if that person has the manage right on
  the parent (listed there, through a group, or as a Document Administrator). The same holds when such a
  folder is moved below another parent and when a folder stops inheriting its access: its owner must
  have the manage right there. A Document Administrator and the superuser are not limited by it.
  Whoever is named as the owner of a folder must be a document user, for everyone but the superuser; the
  owner that an editor's new folder takes from its parent, the owner of a folder that the superuser made
  and an owner who stays are named by nobody and are not asked. A manager who cannot read the parent is
  refused for every owner who needs a grant on the parent, so the answer says nothing about the members
  of a parent that the manager cannot see; a Document Administrator as the new owner needs no grant, and
  that handover is accepted. Such a manager asks a Document Administrator to hand the folder over to
  anyone else. A restricted folder whose current owner has no manage right on its parent (the
  superuser, which owns the folders that a cron job or a script made, or a user who left the document
  group) cannot be moved or restricted again by a manager who is not a Document Administrator, and the
  refusal is the member ceiling message although the cause is the current owner; a write that names a
  valid new owner in the same call is accepted. The owner of a folder that inherits its access is not
  limited, as member rows are not. The rules are asked of the state that the ORM stored (the owner, the
  parent, the access and the company that the folder has after the call) and not only of the values that
  came in, so a value of another type or a default of another kind cannot leave a folder outside them.
  The many2one values of a folder (its company, parent and owner) and the company of a member row are
  judged by the id that the ORM stores: an integer that is not a flag, a single record, an (id, name)
  pair, False and None are accepted, and an empty list or tuple means no record; a list with
  something in it, a text, a dict, a float, True and several records are refused, because the ORM
  would store none of them as the record that a guard might read from them.
  The value that was judged is the value that is written, and after the call the stored company and
  parent are compared with the ones that were authorised; a difference refuses the request.
  The owner of a document is judged the same way, by the id that the ORM stores, whether it is
  given when the document is made or later: an integer that is not a flag, a single user record, an
  (id, name) pair, False and None are read as the ORM reads them, and a value that has no id is
  refused for every caller but the superuser (so is an integer below 1). A caller who is not a Document Administrator may name
  only himself as the owner, and the comparison is made on the stored id, so a number that equals his
  id but is stored as no owner (a float, or True) is not taken for himself. Only a Document
  Administrator may clear the owner with False.
  The company of a member row that has the wrong shape ends in a database error when the row is
  made, and nothing is stored; when the row is changed, the request is refused cleanly.
- **HD-14** Inbound email on a document or a folder is a reply, never a way in. Core posts an
  incoming mail as the system user, which every guard that looks at the posting user lets through, so
  the sender is judged instead: the mail is accepted only when its From address belongs to a partner
  who has an active internal user that can read the document or folder under the record rules (the
  same test as for who is notified). Everybody else is refused with one answer, before anything is
  written (no message, no file, no follower): portal users, unknown senders, contacts without an
  account, a partner whose user is archived or left the document group, and an internal user who
  cannot read the record. The answer does not say which of these it was, nor whether the record
  exists. A mail never creates a document or a folder. The gateway routes to these models stay
  closed to everyone but the mail fetcher and a system administrator. Two more rules hold the
  sender to the mail as it was delivered. The first address of the raw From header must be the
  sender that core resolved from the mail; a From header with defects, or an address without a
  domain, is refused, so a legitimate sender whose header is malformed (for example an unquoted comma
  in the display name) is refused as well. And `message_update` of both models, the method that
  core calls before it posts a reply on an existing record and where the sender is judged, runs only
  for the superuser, which is how core calls it; any other caller is refused with the same answer,
  so the method cannot be used to ask whether somebody may read a record. The sender's address is
  taken as delivered: the module does not check SPF, DKIM or DMARC (see the deployment notes).
  Inbound mail may be active on a site. Behaviour change: a portal contact who answers a notification by email no longer posts
  into the chatter, which the contact could not do over the web either; the reply is refused.
  For the same reason a reply from a personal or secondary address is refused, because the address
  must belong to the partner of an internal user, and a contact without a user that shares the address
  of an internal user can be chosen by core as the author of the mail, so that reply is refused too.
- **HD-15** A description that a caller who is not an internal user writes with a file (a portal
  contact, through the upload) is limited in length and is sanitised a second time without style and
  class attributes, so that it cannot restyle the page of the staff who read it. Text, links and
  images stay. Internal users keep their formatting.

- **HD-16** A document shared with a person as editor can be changed by that person (rename, replace
  the content, tags, description, trash) without any right on its folder. The share gives nothing else:
  the editor cannot move the document, give it to another owner, manage who it is shared with, or
  restore it from the trash, unless the person could do so through the folder before. Whoever may change
  the share lists is exactly who could in 18.0.1 (write on the document and manage level on its folder).
  A portal contact can only be a viewer. Every content replacement keeps the previous content as a
  version, also one made by an editor by share; the server creates that version under a create-only rule.
- **HD-17** An end date of access is enforced by a scheduled action that runs every 30 minutes. A person
  keeps access until the next run after the last day, not until midnight. When a share row or member row
  is removed, the server clears the locks of people who can no longer write the document (it asks in the
  holder's own environment; the only write is clearing the lock, logged as a forced unlock).
- **HD-18** Five places use another environment than the caller's, each registered in the static check
  and documented: the folder mailbox creates documents *as the sender* (it lowers the gateway's rights to
  the sender's own); automatic filing creates the filed document as the superuser, because the person who
  attaches a file is usually no member of the target folder (the rule made by a document administrator is
  the authorisation; policy and quota still apply, failures never break the attachment); filing first
  reads the record in the environment of the maker of the rule, so a rule never files what its maker may
  not read; the lost-lock release described in HD-17; and the removal of the oldest version when a permitted replacement reaches the limit of 20 versions (an editor by share may replace the content but has no right to delete history; the call is refused for a document under legal hold before it is used). Only the holder of a lock, a manager of the folder and a document administrator can unlock; the owner of the document cannot, unless he is one of them.
- **HD-19** Files filed by a rule are visible to everyone who can open the target folder, even to people
  who cannot open the record the file was attached to. The filing rule form says so in plain words.
  A document administrator decides which kinds of record are filed and where.
- **HD-20** A shortcut never gives access to its target, and neither does a requested-file placeholder;
  both are hidden from portal and public users. A person who cannot read a shortcut's target learns
  nothing about it (one neutral message for no access, trashed and gone). Requests grant no right: the
  person asked must already be able to store a file in the folder.
- **HD-21** PDF files are parsed with limits: raw markers are counted before parsing, the page tree is
  walked with a node limit, the chain of cross-reference sections is walked with a link limit, parsing
  has a time budget, and the streams the PDF library decodes are checked against size limits that scale
  with the file. Page, output and input counts are limited (500 pages per source, 100 outputs per split,
  50 inputs and 2,500 pages per merge). Encrypted, damaged, too large and non-PDF content is refused with
  a short message. The pre-checks bound the common forms of hostile files but are not a full PDF parser;
  the known remaining gaps are tracked.
- **HD-22** A wizard row can only name documents and folders that the caller's own search finds; a
  missing and a hidden id get the same refusal, and defaults from the context cannot add an unchecked id.
  The attachment overview of other apps is visible to document users by default and shows exactly what
  the user's own search over attachments returns; a folder that lists the overview group as a member is
  therefore open to all document users (check member lists before upgrading from 18.0.1.0.0).

## Deployment notes

- **Trusted proxies.** Behind a reverse proxy, set the trusted proxy networks and the client IP
  header in the settings. Without them the audit records of requests that came through the proxy
  carry the address of the proxy, not the address of the client.
- **Quota.** With a storage quota set, the files of one upload are checked together, last, and
  the stored bytes are measured once through a database connection of their own after the
  upload took the quota lock. One account has one upload transaction at a time: its other
  requests wait for it (up to five seconds, then 503) and not for the site-wide lock.
- **Indexing.** The files of documents are saved without core indexing: their `index_content`
  stays empty, whether or not `attachment_indexation` is installed. Its indexers unpack and parse
  the members of Office and OpenDocument archives of any file type and any declared size, while
  the upload policy bounds only the directory of an archive and reads a member named `mimetype`
  of any accepted ZIP (a plain zip, Office and OpenDocument files alike) only when it is stored or
  deflated, at most 1 KiB compressed and at most 255 bytes declared; an upload whose member is
  stored another way is refused as of the wrong type. A module that searches the content
  of documents has to decide what it reads from a file, and how much.
- **Inbound mail.** The module trusts the From address as it is delivered, so the mail server of
  the site must enforce SPF, DKIM and DMARC before inbound mail is switched on for documents and
  folders; a spoofed From address of a user who can read a record passes the module's checks. Use
  IMAP for a fetchmail server that can receive replies for these models. With POP, core deletes a
  message from the server only after it was processed, so a refused reply is fetched again on every
  run, and enough refused messages at the head of the mailbox keep later mail from being fetched.
  Do not point a mail alias at these models: a refused new message makes core send a bounce and mark
  the alias as invalid.

- **Folder mailbox.** Allowed senders are the followers of the mailbox record, and at the moment a mail
  arrives the sender must be an active internal document user with upload level on the folder. The module
  trusts the sender address that the gateway delivers, so the inbound mail server must enforce SPF, DKIM
  and DMARC before a mailbox is used; the same rule as for replies (HD-14). The mail gateway must be
  reachable only by the mail fetcher or a system administrator. Nothing of a received mail is kept on the
  mailbox except a short note with counts.
- **Expiry of access.** The scheduled action "Documents: End expired access" must be enabled; if it is
  stopped, end dates are not enforced.
- **Filing rules and requests mail.** Filing rules are limited to document administrators. Reminders of
  requested files need the public address setting used for notices, and send a link that needs a login.
- **Upgrade.** Take a database backup before `-u`; going back means restoring it. Check the member lists
  of folders for the attachment overview group first (HD-22).

## Known limitations (pending owner decisions)

- Folder names are unique per level and company. A user who may create or rename folders at a
  level gets the same "name already used" answer whether the other folder is visible to them or
  not, so they can learn that a name exists there. Removing the rule or limiting it to visible
  folders changes the product rule and is left to the owner.
- Storage usage counts identical stored content once. Uploads are always charged their full size
  and non-administrators do not see the usage percentage, but an uploader who fills the site
  close to its limit could still infer from later answers whether content they uploaded was
  already stored.
- Legal hold protects the held item and every folder whose subtree contains a held item. Items
  trashed on their own inside a held folder follow their own hold.
