# HoH 0.3.19 — role control cards

The installed HoH skills now use progressive disclosure. Each role receives a
short control card containing only its mandatory loop, hard stops and event
router. Detailed, conditional protocol stays in role references and is loaded
only for reconciliation, capability review, candidate recovery, protected merge,
test design or UI QA.

The generated desktop setup prompt follows the same design: it installs the two
role skills, checks exact MCP identity and sends the agent to its control card.
It no longer reproduces the complete role manual inside every new chat prompt.

No runtime authorization or lifecycle rule is weakened. HoH still enforces
identity/project scope, leases, context freshness, capability approval,
reconciliation barriers, candidate identity, QA evidence and protected merge.
The change moves the guidance that helps an agent decide among valid server
paths; it does not move enforcement out of the server.

The original detailed requirements are retained in references, including lease
recovery, worktree provenance, submission retry, QA environment provenance,
capability approval, failure reporting, ticket retirement and merge recovery.
This release deliberately does not run an automated suite under the live-pilot
policy; it validates source compilation, skill structure, generated prompts,
package contents and service upgrade health.
