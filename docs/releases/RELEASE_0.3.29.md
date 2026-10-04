# HoH 0.3.29 — less ceremony (draft)

Draft. Removes duplicated or dead instructions so automated development carries
less to read and less to keep in sync. Addresses H9 and H10 from the
[2026-10 architecture review](../reviews/2026-10-harness-architecture-review.md).

- **Semantic decision records removed.** The queued merge never wrote them and
  the legacy writer went in 0.3.28. Deleted `semantic_record.py`,
  `GitWorktreeManager.commit_semantic_record`, their tests and
  `docs/SEMANTIC_LAYER.md`.
- **H9 — hand-written client prompts and configs removed.** `deploy/codex`,
  `deploy/claude-code` and `deploy/antigravity` registered only the role skill,
  polled every minute, set a 60 s Codex tool timeout against a long wait and used
  a stdio env role. Client configs now come only from `local_agent setup` and
  `hoh-agent-kit`; ROLE_WAKEUPS, ANTIGRAVITY_SETUP, LOCAL_SETUP and AGENT_KIT
  point there. The dead `local_agent._make_prompts_0_3_18_legacy` is gone.
- **H10 — one statement of the wait protocol.** The `work.wait_for_event` tool
  description is authoritative (it now also covers `poll_tool`, replay and
  asking the user). The base skill, `session-start.md`, `wakeup.md`, the kit's
  `START_HERE.md` and the generated setup prompt say one line that points at it.
  A test fails if a skill restates the wait constants.
- **Skill tool-name check.** `test_skill_assets` checks only backticked
  `role.verb` tokens whose prefix is a real tool namespace, so file names and
  field paths are no longer reported as missing tools.

No schema or data changes.
