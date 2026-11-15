# Requested files (release 18.0.2.0.0)

## What it adds for users

A document user can ask a colleague for a file. They give what is needed, the folder, the person
asked, how many days it may take, tags and a short note. The person asked gets an "Upload
Document" activity. They open the request, upload the file, and the request becomes an ordinary
document in that folder. The requester can send a reminder (at most once every 20 hours), and
the requester and anyone else who may write the request can cancel it by moving it to the trash.

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
| `request_last_reminder` | time of the last reminder; a stored field of the document that only the server writes |

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
  the requester owns it (core asks for write access on the document), and then the owner is set to
  the person asked. The existing write rule **R-D2** (the owner of a document in a folder where
  the user may upload) is what lets the person asked store the file. An editor of the folder and a
  document administrator may write it anyway. Rule **R-Q3** (write only) keeps the requester able
  to act on the placeholder they made while it is still a request and they still have upload level
  in its folder: rename, tag, cancel, remind. It cannot change the request fields, the type, or put
  a file on the placeholder (those stay server-only), and it ends when the file arrives. Without it
  the requester could not read the task that core keeps for the person asked, nor cancel.
* Fulfil: whoever may write the placeholder (the person asked, an editor, an administrator). The
  file goes through the upload policy and the quota like any upload. The activity is marked done
  with a note, `request_done` is logged.
* The task of a request is completed only by sending the file. `mail.activity._action_done` refuses
  a task that a placeholder points to unless `_woow_fulfil_request` passes its in-process marker,
  so core's "Upload Document" button (which would store a file outside the upload policy) and
  "Mark as done" cannot end the task and leave a pending placeholder. Other activities, also
  "Upload Document" ones on other documents, are untouched. The fulfilment also checks that the task
  still belongs to the placeholder and to the person asked before it completes it (its creator
  can write it in core), so a retargeted task cannot post on another record.
* Reminder (`action_send_reminder`): the requester, or a user with manage level. Queued mail, no
  access code, a link that needs a login. At most one per 20 hours: the time is kept in
  `request_last_reminder`, which no client can write. A pending request that has no task (restored,
  or the task was removed) gets a new one with the reminder.
* Cancel: trashing the placeholder (`action_trash`), which core does for the activities of an
  archived record. The person asked, the requester (R-Q3), an editor of the folder and an
  administrator can. Restoring a cancelled request (`action_restore`) schedules a new task for the
  person asked when they can still store a file there (the restore wizard path does not; the
  reminder then makes the task).
* Share (portal) users never see a placeholder: global rule **R-Q0**. Wizard rows belong to their
  creator: **R-Q1**, **R-Q2**. The write rule for the requester: **R-Q3**.

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
* `_woow_open_request` and `_woow_fulfil_request` write through `super(WoowDocument, ...)` and so
  skip an extension of `woow.document` that is loaded after `models/document.py`. The integrator
  must check that this bypass is intended for the lock, version and share logic of other lanes.
* The document form hides the Type choice "Requested file" on a new document (an onchange resets
  it); a request is only made with "Ask for a file".
