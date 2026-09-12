# Portable agent kit

The service must run the matching release with an operator-configured repository,
project scope and bearer identity. Install the wheel on the **service host**;
HTTP clients install only the skills and MCP configuration.

```sh
python -m pip install hoh_control_plane-0.3.0-py3-none-any.whl
hoh-agent-kit --role developer --server-url https://hoh.example.com/mcp --output ./developer-kit
```

The builder emits a directory and ZIP. It includes the base skill, selected role
skill and references, a checksum manifest, `START_HERE.md`, a stdlib Python
installer, and Codex/Claude Code HTTP configurations. It refuses URLs containing
credentials or query strings. Token values are never read or embedded.

After unpacking on the agent machine:

```sh
python install.py --client codex
# or: python install.py --client claude-code
```

Existing unrelated settings are retained and changed config files are backed up.
Repeating an identical install is safe. Different HoH settings/skills are refused
for review before any write. Use `--home <directory>` for an isolated client home.
The examples use `HOH_AGENT_TOKEN`; set it in the actual client process environment
using the operator-issued credential, then restart the client. This release
supports static bearer authentication, not an OAuth browser login service.

Start with: “Use $hoh-control-plane and $hoh-developer. Call session.bootstrap and
continue my authorized project work; resume an existing lease/job first.”
Choose the role matching your credential. A client without filesystem/Git/shell
capabilities can plan/observe but cannot locally implement code merely by authenticating.

For colocated development, the agent must access the service's assigned worktree.
For agent_workspace, the operator must supply Git access and a delivery/fetch path
that makes submitted SHAs reachable in the service repository. The kit does not
copy private repositories or credentials. Install the QA/browser dependencies on
the service runner for remote QA commands.

## Server ingress

Use `HOH_MCP_TRANSPORT=streamable-http`, configure `HOH_MCP_TOKENS_JSON` securely,
and keep `HOH_MCP_HOST=127.0.0.1` behind a TLS proxy. Add the public hostname to
`HOH_MCP_ALLOWED_HOSTS`, e.g. `hoh.example.com`. If a browser MCP client sends an
Origin, set the exact permitted origins in `HOH_MCP_ALLOWED_ORIGINS` too. These
are comma-separated allowlists. Streamable HTTP is stateless at the transport
layer; task state remains durable in the database. Every request is authenticated.

Use one identity per concurrent worker. Reusing a token in another chat is a
resume only after the previous worker stopped. Presence heartbeat differs from
lease heartbeat; a stopped chat needs a host scheduler/wakeup adapter.

The historical `deploy/codex` and `deploy/claude-code` files are host-specific
examples. They are not copied into portable kits.

See [official Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
for `bearer_token_env_var`; keep secrets out of static headers and chat history.
