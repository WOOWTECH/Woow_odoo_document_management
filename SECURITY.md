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
  one, are answered with 429 until the minute ends. The site keeps a per-minute capacity for the
  records of refused link requests: each new per-source counter and each new audit record (kept
  per source, link and minute) uses one slot. Wrong passwords use none, since each link counts at
  most 50 of them per hour. When the capacity is used up, a source's first refused request of the
  minute is answered with 429 and not recorded, later refused requests of sources already counted
  still count, and valid links, unlocked sessions and password attempts are not affected.
  *(text pending)*
- **HD-07** The portal folder write right is only an upload probe; real writes are refused.
  *(text pending)*
- **HD-08** A queued share mail containing a link exists in the database until it is sent, or for
  at most 24 hours after a failed send, readable only by system administrators. *(text pending)*
- **HD-09** System administrators can obtain superuser power; this is outside the module's scope.
  *(text pending)*
- **HD-10** Messages sent with "Send message" on a folder with portal members are mailed to every
  follower; a reminder is shown above the chatter. *(text pending)*
- **HD-11** The share code lives in the URL fragment and may briefly remain in browser history.
  *(text pending)*
- **HD-12** Chatter attachments keep Odoo's native content-type rules; every streamed response
  carries a `default-src 'none'` content security policy. *(text pending)*
