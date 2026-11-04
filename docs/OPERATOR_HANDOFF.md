# Operator handoff

How operator work moves between sessions and machines (a local Windows
checkout, a cloud container). This file holds the rules; the state of a task
lives in one comment on GitHub, so local and cloud sessions read the same copy.

## Where the state lives

- One **handoff comment** per task: on the task's pull request, or on its issue
  when there is no pull request yet. Its first line is
  `<!-- hoh-operator-handoff -->`. Search the comments for that marker before
  posting; edit the one you find, never post a second one. GitHub keeps the
  earlier versions in the comment's edit history.
- One session writes it at a time; the comment names that session. Read the
  current comment again immediately before editing, so another session's update
  is not overwritten.
- The code is the pushed commit the comment names. Ticket state is never copied
  into it: it lives in the control plane (`work.discover`, see `AGENTS.md`).

## Required fields

| Field | Content |
| --- | --- |
| Revision | Branch and full SHA; whether it is pushed. |
| Goal | What the task is for, in one or two sentences. |
| Decisions | What the operator decided, with the date. |
| Done | Each completed change with its evidence. |
| Open | Gaps and known limits, each stated as such. |
| Authority | What this task may do (commit, push, open a PR) and what it may not (merge, deploy, change ticket state). |
| Next step | The single next action and who must approve it. |
| Updated | Full UTC timestamp and the identity of the session or machine that wrote it. |

A state read from GitHub or a machine (mergeable, checks running, a PR being a
draft) is an observation: write it with the time it was observed, not as a
condition that stays true.

## Evidence

- CI: run URL, job, the SHA it tested, artifact name and its upload SHA256.
  Evidence belongs to the SHA it ran on; a later commit needs its own.
- Local-only evidence: the path and the words **not transferred**. A result a
  session did not check itself is **reported**, never verified.
- A draft that exists only in a session's scratch space is lost with the
  session; say so, or commit it once approved.

## Never in a handoff

Credentials or tokens; database files or dumps; raw evidence that has not been
screened for secrets or personal data; live ticket state; a local absolute path
as the only pointer to something another machine needs.

## Starting a session from a handoff

1. Read `AGENTS.md`, then the handoff comment.
2. Check the revision, the source and the evidence links first: the named SHA
   is on the remote branch, the checkout is at it, and each link resolves to
   that SHA. Report any mismatch before acting.
3. Do not re-run tests only because the session changed. Run them when the
   source changed, the evidence no longer applies, or there is a new question
   to verify.
4. Treat every claim as reported until this session has checked it.
5. Update the comment when the task's state changes, not after every command.
