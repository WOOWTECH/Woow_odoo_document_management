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
| 18.0.1.x | 18.0 Community | yes |

## Deliberate security design decisions (HD)

These are decisions we made on purpose; reports that only restate them are not vulnerabilities.
The full wording is supplied by the lanes that own each area (HD-01, 02, 03, 04, 07, 09, 10, 12, 13:
core models and access; HD-05: trash). HD-06, HD-08 and HD-11 are retired: they described link
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
  access are no longer notified, unless a message names them as recipients. *(text pending)*
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
  the upload policy bounds only the directory of an archive. A module that searches the content
  of documents has to decide what it reads from a file, and how much.

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
