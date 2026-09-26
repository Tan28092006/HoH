# HoH 0.3.24 — a waiting role holds one long wait and stays silent

Delivers HOH-275.

## What was happening

The live marketing roles were watched for seven minutes on 2026-09-26. Every
Planner wait ended `timed_out` after about 54 seconds with no event, followed by
`session.bootstrap` and another wait, roughly every 70 seconds. Earlier
Integrator waits ended the same way after about three minutes. The Codex client
timeout for these profiles is 86,400 seconds, so the client was not cutting the
call: the agents themselves passed a short `timeout_seconds`. The setup prompt
since 0.3.19 names no value, and a model left to choose one picks something that
feels reasonable, like a minute.

Each empty return was narrated ("still waiting"), then called again. The
narration grew the chat's context until the chat ended its turn. A chat that has
ended its turn is not waiting at all, so the role went offline, which is what the
operator saw as the chat "turning itself off".

Event delivery itself was healthy throughout: when the marketing Developer
submitted a candidate, QA and Planner were woken three seconds later.

## What changed

- `work.wait_for_event` over MCP raises any shorter `timeout_seconds` to the
  server maximum of 12 hours. An event still returns the wait at once, so a long
  hold costs nothing when work exists. The value is only ever raised, so an
  invalid one still reaches validation and is refused.
- A wait that returns without an event carries `next_action`: call the wait again
  now, same project, and write nothing to the chat. The instruction arrives in
  the result the model is reading at the moment it decides what to do next.
- The common card gains step 6 and every generated setup prompt gains step 7:
  while waiting, write nothing; an empty or resumed wait is not news; speak only
  for an event to act on, a superseded wait, an error, or a question only the
  user can answer.
- The tool description says the same, and the session-start reference no longer
  tells agents to pass `timeout_seconds`.

The hold is enforced by the server, so it applies to chats that are already
running, before any client reload. The card and prompt changes reach a chat when
it reloads its skills or a new chat is set up.

## Tests

- New `tests/test_silent_wait.py`: the hold for 1, 60, 180 and 3,599 seconds,
  the default, pass-through of an invalid value, and the silent `next_action`.
- New prompt test in `tests/test_desktop_controls.py` for the card and every
  role prompt.
- Three transport tests that exercise progress, fencing and resume with a
  one-second timeout now lower the hold explicitly, rather than depending on it.
- Wait-related modules: 118 passed. The two failures are in the pre-existing set
  on `main` (`test_planner_receives_new_intervention_job`,
  `test_skills_only_name_registered_mcp_tools`). The full suite was not run.
