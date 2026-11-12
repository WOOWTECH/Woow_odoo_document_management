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
The full wording is supplied by the lanes that own each area (HD-01, 06, 08, 11: share links;
HD-02, 03, 04, 07, 09, 10, 12: core models and access; HD-05: trash).

- **HD-01** Native attachment download routes follow the same record rules but are not audited.
  *(text pending)*
- **HD-02** Internal readers of a folder can see its members and effective access. *(text pending)*
- **HD-03** Readers of a folder can see the numeric ids of its ancestors. *(text pending)*
- **HD-04** Chatter attachments on documents and folders follow Odoo's native upload rules.
  *(text pending)*
- **HD-05** The "restricted subfolder" flag is visible so that subtrees cannot be trashed blindly.
  *(text pending)*
- **HD-06** Rate limiting depends on the client IP; an IPv6 /64 counts as one source. Per-source
  limits count refused link requests and wrong passwords, each up to the per-minute limit. They
  never refuse opening a valid link or using an unlocked session, but once a source has used its
  per-minute limit of wrong passwords, its password attempts on every link, including a correct
  one, are answered with 429 until the minute ends. Each link also limits wrong passwords per
  source (its cooldown) and to 50 per hour from all sources. The site keeps a per-minute capacity
  for the records of refused link requests: each new per-source counter and each new audit record
  (kept per source and minute, whichever link was asked for) uses one slot, so neither the
  answers nor the capacity left tell whether a link exists. Wrong passwords use none, since each
  link counts at most 50 of them per hour. When the capacity is used up, a source's first refused
  request of the minute is answered with 429 and not recorded, later refused requests of sources
  already counted still count, and valid links, unlocked sessions and password attempts are not
  affected. Refused link requests are counted with one statement per count, in a short
  transaction of their own, so the requests of one source never wait for each other, never fail
  for each other and are never run again: exactly the limit's worth are counted and the others
  are answered with 429. When the second database connection for that count cannot be had in
  time (a full connection pool or a long wait, see Deployment notes), the refused request is
  answered with 429 and nothing is recorded, never with a server error.
  *(text pending)*
- **HD-07** The portal folder write right is only an upload probe; real writes are refused.
  *(text pending)*
- **HD-08** A queued share mail containing a link exists in the database until it is sent, or for
  at most 24 hours after a failed send, readable only by system administrators. *(text pending)*
- **HD-09** System administrators can obtain superuser power; this is outside the module's scope.
  *(text pending)*
- **HD-10** Messages sent with "Send message" on a folder with portal members are mailed to every
  follower who can still read the folder or document, and to contacts without an account that
  staff added as followers; a reminder is shown above the chatter. Followers whose account lost
  access are no longer notified, unless a message names them as recipients. *(text pending)*
- **HD-11** The share code lives in the URL fragment and may briefly remain in browser history.
  *(text pending)*
- **HD-12** Chatter attachments keep Odoo's native content-type rules; every streamed response
  carries a `default-src 'none'` content security policy. *(text pending)*

## Deployment notes

- **Database connections.** Counting a refused link request (HD-06) uses a short second database
  transaction, so such a request can need two connections of the
  `db_maxconn` pool at the same moment. The addon keeps at most `db_maxconn // 32` of these
  reservations (at least one) open at a time in a process and makes the others wait up to half
  a second, so they cannot take the connections that the other requests need. Run Odoo with
  prefork workers (`workers` greater than 0), where each worker serves one request at a time.
  With `workers = 0`, limit the threads: set `ODOO_MAX_HTTP_THREADS` in the server's environment
  to an integer of at most `(db_maxconn - max_cron_threads) // 2`, or to any non-integer value
  such as `auto`, which makes Odoo use that number. Without a limit a threaded server accepts
  more simultaneous requests than the pool has connections, and a burst of requests can leave
  any route, valid share links included, without a connection (a limit of Odoo itself, not
  specific to this addon). When the second connection or its turn is not available the addon
  answers the refused request with 429 and records nothing; it does not answer with a server
  error.
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
