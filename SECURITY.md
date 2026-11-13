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
The full wording is supplied by the lanes that own each area (HD-01, 02, 03, 04, 07, 09, 10, 12:
core models and access; HD-05: trash). HD-06, HD-08 and HD-11 are retired: they described link
sharing, which is not part of this module. Retired numbers are not reused.

- **HD-01** Native attachment download routes follow the same record rules but are not audited.
  *(text pending)*
- **HD-02** Internal readers of a folder can see its members and effective access. *(text pending)*
- **HD-03** Readers of a folder can see the numeric ids of its ancestors. *(text pending)*
- **HD-04** Chatter attachments on documents and folders follow Odoo's native upload rules.
  *(text pending)*
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
  carries a `default-src 'none'` content security policy. *(text pending)*

## Deployment notes

- **Trusted proxies.** Behind a reverse proxy, set the trusted proxy networks and the client IP
  header in the settings. Without them every visitor counts as one source, the address of the
  proxy, and their audit records carry that address.
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
