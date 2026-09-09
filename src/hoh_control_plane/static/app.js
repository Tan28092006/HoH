const state = { projectId: "demo", board: null, activity: null, dispatch: null, selectedTicketId: null };

const statusColumns = [
  ["READY", "Ready"],
  ["DEV_IN_PROGRESS", "In development"],
  ["QA_READY", "Awaiting QA"],
  ["QA_IN_PROGRESS", "QA in progress"],
  ["REWORK", "Rework"],
  ["VERIFIED", "Verified"],
  ["BLOCKED", "Blocked"],
  ["DONE", "Done"],
];

const boardElement = document.querySelector("#board");
const detailElement = document.querySelector("#detail");
const errorBanner = document.querySelector("#error-banner");
const activityHeaders = { "X-HOH-Agent-Id": "ui-observer", "X-HOH-Role": "planner" };

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

function initials(value) {
  if (!value) return "–";
  return value.split(/[-\s]/).filter(Boolean).map(part => part[0]).join("").slice(0, 2).toUpperCase();
}

function showError(message) {
  errorBanner.textContent = message;
  errorBanner.hidden = false;
}

function clearError() { errorBanner.hidden = true; }

async function loadDashboard() {
  clearError();
  const boardRequest = fetch(`/api/v1/projects/${encodeURIComponent(state.projectId)}/board`);
  const activityRequest = fetch(
    `/api/v1/projects/${encodeURIComponent(state.projectId)}/agent-activity?limit=100`,
    { headers: activityHeaders },
  );
  const dispatchRequest = fetch(
    `/api/v1/projects/${encodeURIComponent(state.projectId)}/dispatch`,
    { headers: activityHeaders },
  );
  const [boardResponse, activityResponse, dispatchResponse] = await Promise.all([boardRequest, activityRequest, dispatchRequest]);
  if (!boardResponse.ok) throw new Error(`Board request failed (${boardResponse.status})`);
  state.board = (await boardResponse.json()).data;
  if (activityResponse.ok) {
    state.activity = (await activityResponse.json()).data;
  } else {
    state.activity = { calls: [], unavailable: `Activity log requires an authenticated dashboard (${activityResponse.status}).` };
  }
  if (dispatchResponse.ok) {
    state.dispatch = (await dispatchResponse.json()).data;
  } else {
    state.dispatch = { agents: [], queues: [], unavailable: `Dispatch requires an authenticated dashboard (${dispatchResponse.status}).` };
  }
  document.querySelector("#project-description").textContent = state.board.project.description;
  document.querySelector("#connection-label").textContent = "Live";
  renderBoard();
  renderFeatures();
  renderDispatch();
  renderActivity();
}

function renderBoard() {
  const populated = statusColumns.filter(([status]) => (state.board.columns[status] || []).length || ["READY", "DEV_IN_PROGRESS", "QA_READY", "REWORK"].includes(status));
  boardElement.innerHTML = populated.map(([status, label]) => {
    const tickets = state.board.columns[status] || [];
    return `<section class="column" aria-label="${escapeHtml(label)}">
      <div class="column-head">${escapeHtml(label)}<span>${tickets.length}</span></div>
      ${tickets.map(ticketCard).join("")}
    </section>`;
  }).join("");
  boardElement.querySelectorAll(".ticket").forEach(button => {
    button.addEventListener("click", () => selectTicket(button.dataset.ticketId));
  });
  if (state.selectedTicketId) selectTicket(state.selectedTicketId);
}

function ticketCard(ticket) {
  const agent = ticket.lease_agent_id;
  const selected = state.selectedTicketId === ticket.ticket_id ? " selected" : "";
  return `<button class="ticket${selected}" data-ticket-id="${escapeHtml(ticket.ticket_id)}">
    <span class="ticket-top"><span class="ticket-key">${escapeHtml(ticket.ticket_id)}</span><span class="badge">${escapeHtml(ticket.feature_title)}</span></span>
    <span class="ticket-title">${escapeHtml(ticket.title)}</span>
    <span class="ticket-foot"><span class="meta">${ticket.evidence_count} evidence · ${ticket.dependency_count} dependencies</span><span class="avatar">${escapeHtml(initials(agent))}</span></span>
  </button>`;
}

function selectTicket(ticketId) {
  const tickets = Object.values(state.board.columns).flat();
  const ticket = tickets.find(item => item.ticket_id === ticketId);
  if (!ticket) return;
  state.selectedTicketId = ticketId;
  boardElement.querySelectorAll(".ticket").forEach(button => button.classList.toggle("selected", button.dataset.ticketId === ticketId));
  const relatedCalls = (state.activity?.calls || []).filter(call => {
    const target = call.input_summary?.target || {};
    return target.ticket_id === ticket.ticket_id || target.ticket_ref === `ticket:${ticket.ticket_id}`;
  });
  detailElement.innerHTML = `<p class="eyebrow">${escapeHtml(ticket.ticket_id)}</p>
    <h2>${escapeHtml(ticket.title)}</h2>
    <span class="badge">${escapeHtml(ticket.status)}</span>
    <div class="detail-grid">
      <div class="detail-stat"><span class="meta">Role</span><b>${escapeHtml(ticket.required_role)}</b></div>
      <div class="detail-stat"><span class="meta">Version</span><b>${ticket.version}</b></div>
      <div class="detail-stat"><span class="meta">Lease</span><b>${escapeHtml(ticket.lease_agent_id || "Unassigned")}</b></div>
      <div class="detail-stat"><span class="meta">Candidate</span><b>${escapeHtml(ticket.candidate_commit ? ticket.candidate_commit.slice(0, 8) : "None")}</b></div>
    </div>
    <h3>Objective</h3><p>${escapeHtml(ticket.objective)}</p>
    <h3>Acceptance gate</h3><ul>${ticket.acceptance_criteria.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
    <h3>Preservation constraints</h3><ul>${ticket.preservation_constraints.map(item => `<li>${escapeHtml(item)}</li>`).join("") || "<li>None</li>"}</ul>
    <h3>Write scope</h3><code>${escapeHtml(ticket.file_scope.join(", ") || "Not defined")}</code>
    <h3>Agent calls</h3><p class="meta">${relatedCalls.length ? relatedCalls.map(call => `${escapeHtml(call.actor_id)} · ${escapeHtml(call.tool_name)}`).join("<br>") : "No MCP activity has targeted this ticket yet."}</p>`;
}

function renderFeatures() {
  document.querySelector("#feature-list").innerHTML = state.board.features.map(feature => `<article class="feature"><p class="eyebrow">${escapeHtml(feature.feature_id)}</p><h3>${escapeHtml(feature.title)}</h3><p>${escapeHtml(feature.objective)}</p><p>Context: <code>ctx:feature:${escapeHtml(feature.feature_id)}:v${feature.version}</code></p></article>`).join("");
}

function renderDispatch() {
  const presence = document.querySelector("#presence-list");
  const list = document.querySelector("#dispatch-list");
  if (state.dispatch?.unavailable) {
    presence.innerHTML = "";
    list.innerHTML = `<article class="event"><strong>Dispatch unavailable</strong><span>${escapeHtml(state.dispatch.unavailable)}</span></article>`;
    return;
  }
  const agents = state.dispatch?.agents || [];
  const queues = state.dispatch?.queues || [];
  presence.innerHTML = agents.length ? agents.map(agent => `<article class="agent-card">
    <span class="presence ${agent.online ? "online" : "offline"}"></span><span class="avatar">${escapeHtml(initials(agent.agent_id))}</span>
    <div><b>${escapeHtml(agent.agent_id)}</b><p>${escapeHtml(agent.role)} · ${agent.online ? "available" : "offline"} · capacity ${agent.capacity}</p></div>
  </article>`).join("") : `<p class="empty-detail">No execution agent has checked in yet. Their ticket hand-offs stay pending.</p>`;
  list.innerHTML = queues.length ? queues.map(queue => `<article class="dispatch-item">
    <span class="dispatch-state ${escapeHtml(queue.status)}">${escapeHtml(queue.status)}</span>
    <div><strong>${escapeHtml(queue.ticket.ticket_id)} · ${escapeHtml(queue.ticket.title)}</strong><p>Target: ${escapeHtml(queue.target_role)} · ticket context v${queue.context_version} · ${queue.open_offer_count} open offer${queue.open_offer_count === 1 ? "" : "s"}</p></div>
    <span class="target">${escapeHtml(queue.claimed_by || "Waiting for a claim")}</span>
  </article>`).join("") : `<article class="event"><strong>No queued hand-offs</strong><span>Ready tickets will appear here until a Developer or QA obtains the exclusive lease.</span></article>`;
}

function describeTool(toolName) {
  const actions = {
    "identity.get_capabilities": "checked its role and allowed capabilities",
    "agent.register": "checked in as available for dispatch",
    "agent.heartbeat": "renewed its dispatch availability",
    "work.inbox": "checked its durable role-matched work inbox",
    "work.discover": "looked for actionable or blocked work",
    "feature.get_context": "read feature progress and context",
    "ticket.get_context": "read ticket context, dependencies and evidence",
    "planner.create_project": "created a project",
    "planner.configure_repository": "configured the project's Git repository",
    "planner.configure_test_command": "registered the project test command",
    "planner.create_feature": "created or split a feature",
    "planner.create_ticket": "planned a bounded ticket",
    "planner.set_ticket_ready": "released a ticket to the execution queue",
    "planner.append_context": "recorded a planning decision",
    "work.claim": "claimed a Developer ticket lease",
    "dev.prepare_worktree": "prepared an isolated Git worktree for the ticket",
    "work.heartbeat": "renewed its lease",
    "dev.append_progress": "recorded a development checkpoint",
    "dev.report_blocker": "reported a blocker to planning",
    "dev.submit_candidate": "submitted a frozen candidate for QA",
    "qa.claim_candidate": "claimed a frozen candidate for QA",
    "qa.run_project_tests": "ran the Planner-registered test command on the frozen candidate",
    "qa.record_test_run": "recorded a QA test run",
    "qa.submit_verdict": "submitted an evidence-backed QA verdict",
  };
  return actions[toolName] || "called an MCP tool";
}

function targetLabel(call) {
  const target = call.input_summary?.target || {};
  return target.ticket_id || target.feature_id || target.project_id || target.ticket_ref || target.feature_ref || "control plane";
}

function renderActivity() {
  const calls = state.activity?.calls || [];
  const summary = document.querySelector("#agent-summary");
  const list = document.querySelector("#event-list");
  if (state.activity?.unavailable) {
    summary.innerHTML = "";
    list.innerHTML = `<article class="event"><strong>Activity unavailable</strong><span>${escapeHtml(state.activity.unavailable)}</span></article>`;
    return;
  }
  const agents = new Map();
  calls.forEach(call => {
    const key = `${call.actor_id}:${call.actor_role}`;
    agents.set(key, (agents.get(key) || 0) + 1);
  });
  summary.innerHTML = agents.size ? [...agents.entries()].map(([key, count]) => {
    const [agentId, role] = key.split(":");
    return `<article class="agent-card"><span class="avatar">${escapeHtml(initials(agentId))}</span><div><b>${escapeHtml(agentId)}</b><p>${escapeHtml(role)} · ${count} MCP calls</p></div></article>`;
  }).join("") : `<p class="empty-detail">Waiting for Planner, Developer or QA agents to call MCP tools.</p>`;
  list.innerHTML = calls.length ? [...calls].reverse().map(call => `<article class="event tool-call">
    <span class="outcome ${escapeHtml(call.outcome)}">${escapeHtml(call.outcome)}</span>
    <div><strong>${escapeHtml(call.actor_id)} <span class="role-label">${escapeHtml(call.actor_role)}</span></strong><p>${escapeHtml(describeTool(call.tool_name))} · <code>${escapeHtml(call.tool_name)}</code></p></div>
    <span class="target">${escapeHtml(targetLabel(call))}</span>
    <time>${new Date(call.occurred_at).toLocaleString()}</time>
  </article>`).join("") : `<article class="event"><strong>No MCP calls yet</strong><span>Ticket progress appears here as agents work through Planner → Developer → QA.</span></article>`;
}

document.querySelector("#refresh-button").addEventListener("click", () => loadDashboard().catch(error => showError(error.message)));
document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item === button));
  ["board", "features", "dispatch", "events"].forEach(view => { document.querySelector(`#${view}-view`).hidden = view !== button.dataset.view; });
}));

loadDashboard().catch(error => { document.querySelector("#connection-label").textContent = "Offline"; showError(error.message); });
window.setInterval(() => loadDashboard().catch(() => {}), 10000);
