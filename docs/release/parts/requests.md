# Requested files (release 18.0.2.0.0)

## What it adds for users

A document user can ask a colleague for a file. They give what is needed, the folder, the person
asked, how many days it may take, tags and a short note. The person asked gets an "Upload
Document" activity. They open the request, upload the file, and the request becomes an ordinary
document in that folder. The requester can send a reminder (at most once every 20 hours), and
anyone who may write the request can cancel it by moving it to the trash.

Where to find it: the **Ask for a file** button on a folder form (members with upload level), the
**Action** menu of the documents list, and the filters **Requested files** and **Asked of me** in
the documents search. A pending request shows a banner on the document form with the buttons
**Upload the file**, **Send reminder** and **Cancel request**.

## Data model

Extension of `woow.document` (file `models/requests.py`):

| Field | Meaning |
|---|---|
| `document_type = 'request'` | a placeholder without content |
| `request_user_id` | the person asked |
| `request_deadline` | due date (today plus the days given, 1 to 365) |
| `request_note` | short text, at most 500 characters |
| `request_activity_id` | the activity of the person asked |
| `request_last_reminder` | read-only; kept on the activity (`mail.activity.woow_request_last_reminder`) |

All five are written by the server only: a client that passes one of them to `create` or `write`
gets an `AccessError`, and a placeholder can only be created in process (the creation path needs an
in-process marker that no RPC call can send). Wizards (transient): `woow.document.request.wizard`
(ask) and `woow.document.request.fulfil.wizard` (send the file).

No new persistent model: `WOOW_MODELS` is unchanged. The new ACL rows belong to the two wizards.

## Who may do what

* Ask: an internal document user with upload level in the folder. The person asked must be an
  active internal document user of the folder's company who can store a file there under the
  existing rules (upload level or more, or document administrator); otherwise the request is
  refused. Nobody can ask themselves. No right is ever granted by a request.
* Ownership: the placeholder is created in the requester's name, the activity is scheduled while
  the requester may write it (core asks for write access on the document), and then the owner is
  set to the person asked. The existing write rule **R-D2** (the owner of a document in a folder
  where the user may upload) is what lets the person asked store the file. An editor of the folder
  and a document administrator may write it anyway.
* Fulfil: whoever may write the placeholder (the person asked, an editor, an administrator). The
  file goes through the upload policy and the quota like any upload. The activity is marked done
  with a note, `request_done` is logged.
* Reminder (`action_send_reminder`): the requester, or a user with manage level. Queued mail, no
  access code, a link that needs a login.
* Cancel: trashing the placeholder (`action_trash`), which core does for the activities of an
  archived record. Because the person asked owns the placeholder, a requester who has only upload
  level in the folder cannot trash it (the existing write rule); the person asked, an editor of the
  folder and an administrator can.
* Share (portal) users never see a placeholder: global rule **R-Q0**. Wizard rows belong to their
  creator: **R-Q1**, **R-Q2**.

## Public methods

`woow.document`: `action_open_fulfil_wizard`, `action_send_reminder`;
`woow.document.request.wizard`: `action_create`; `woow.document.request.fulfil.wizard`:
`action_fulfil`. Each starts with an access check. The creation and the fulfilment themselves are
private (`_woow_open_request`, `_woow_fulfil_request`).

## Audit

`request_create` (detail: days) and `request_done` (detail: bytes) in the audit log; the trash of a
cancelled request is logged as `trash`. No names, addresses or file content in the detail.

## Administrator notes

* The reminder mail needs a public HTTPS address for document notices (the same setting as the
  folder member notice) and a sender address on the requester or the company.
* The reminder template `mail_template_woow_document_request_reminder` is `noupdate`.
* Uninstalling removes the placeholders with the other documents (`ondelete cascade` of the new
  document type).
* A restored placeholder has no activity any more: the person asked can still send the file, and a
  new request can be made.
