# Antigravity role workers

Antigravity loads a workspace-local MCP configuration from
`.agents/mcp_config.json` and supports remote (HTTP) MCP servers. See the
[official MCP configuration documentation](https://antigravity.google/docs/mcp).

HoH ships no hand-written Antigravity templates. Build a portable kit for the
role with `hoh-agent-kit` (see [AGENT_KIT.md](AGENT_KIT.md)), install its skill
folders, and add one HTTP MCP entry using the kit's server URL and token
environment variable.

## Safety model

Use one Antigravity workspace/window and one MCP entry for one role. Do **not**
combine Planner, Developer, QA and Integrator servers in one `mcp_config.json`:
that makes one agent capable of crossing workflow boundaries. Each concurrently
running worker needs its own credential and identity.

## First message

Start the session with: "Use $hoh-control-plane and $hoh-<role>. Call
session.bootstrap and continue my authorized project work; resume an existing
lease/job first." A role or identity mismatch in the bootstrap result means stop
and fix the configuration, never work under another role. Idle waiting follows
the `work.wait_for_event` tool description; do not set up a polling loop.
