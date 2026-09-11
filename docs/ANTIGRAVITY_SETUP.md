# Antigravity role workers

Antigravity can load a workspace-local MCP configuration from
`.agents/mcp_config.json`. It supports local stdio and remote MCP servers. See
the [official MCP configuration documentation](https://antigravity.google/docs/mcp).

## Safety model

Use one Antigravity workspace/window and one MCP profile for one role. Do **not**
combine Planner, Developer, QA and Integrator server entries in one
`mcp_config.json`: that makes one agent capable of crossing workflow boundaries.

Each simultaneously running worker must have a different `HOH_AGENT_ID`. The
templates use `*-antigravity-01`; copy and rename the ID for a second worker,
for example `developer-antigravity-02`.

The current local control plane uses SQLite. Limit concurrent workers until the
merge-worker contention fix is deployed. For a multi-machine deployment, use a
single remote control-plane service backed by the planned shared database rather
than exposing the same SQLite file across machines.

## Install one role

1. Open a separate Antigravity workspace/window for the role. A Developer may
   start at `D:\HoH`; it must still use the ticket-bound worktree returned by
   MCP. QA never edits that worktree.
2. Create that workspace's `.agents` directory.
3. Copy exactly one matching template below to
   `.agents\mcp_config.json`:

   | Role | Template | Session prompt |
   | --- | --- | --- |
   | Planner | `deploy\antigravity\hoh-planner.mcp_config.json` | `deploy\antigravity\prompts\planner.md` |
   | Developer | `deploy\antigravity\hoh-developer.mcp_config.json` | `deploy\antigravity\prompts\developer.md` |
   | QA | `deploy\antigravity\hoh-qa.mcp_config.json` | `deploy\antigravity\prompts\qa.md` |
   | Integrator | `deploy\antigravity\hoh-integrator.mcp_config.json` | `deploy\antigravity\prompts\integrator.md` |

4. In Antigravity, open **Agent panel → … → MCP Servers → Manage MCP Servers**,
   confirm the HoH server is connected, and approve it only after reviewing the
   role and agent ID.
5. Paste the matching prompt as the first message. The agent must call
   `identity.get_capabilities`; a role/identity mismatch means stop and fix the
   MCP configuration, never work under another role.

Do not use automatic loops for a newly installed worker. First validate one
manual `work.inbox` cycle and a single ticket handoff. A durable queue preserves
unclaimed work; a polling loop cannot repair a failed MCP transport.

## Example copy command

Run this from the chosen role workspace, substituting the role template:

```powershell
New-Item -ItemType Directory -Force .agents
Copy-Item D:\HoH\deploy\antigravity\hoh-developer.mcp_config.json .agents\mcp_config.json
```

The templates deliberately point their process `cwd` at `D:\HoH`, where the
control-plane package and local database live. The Antigravity workspace itself
can be a ticket worktree; the MCP server path must remain the trusted control
plane root.
