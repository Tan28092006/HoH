const initialParams = new URLSearchParams(window.location.search);
const requestedProjectId = initialParams.get("project");
const requestedFeatureId = initialParams.get("feature");
const requestedStatus = initialParams.get("status");
const state = {
  projectId: requestedProjectId || "demo",
  board: null,
  activity: null,
  dispatch: null,
  selectedTicketId: null,
  featureFilter: requestedFeatureId || "all",
  statusFilter: requestedStatus || "all",
};

const statusColumns = [
  ["BACKLOG", "Backlog"],
  ["READY", "Ready"],
  ["DEV_IN_PROGRESS", "In development"],
  ["QA_READY", "Awaiting QA"],
  ["QA_IN_PROGRESS", "QA in progress"],
  ["REWORK", "Rework"],
  ["VERIFIED", "Verified"],
  ["MERGE_READY", "Integration"],
  ["BLOCKED", "Blocked"],
  ["DONE", "Done"],
];

const boardElement = document.querySelector("#board");
const detailElement = document.querySelector("#detail");
const errorBanner = document.querySelector("#error-banner");
const projectPicker = document.querySelector("#project-picker");
const featureFilterElement = document.querySelector("#feature-filter");
const statusFilterElement = document.querySelector("#status-filter");
const activityHeaders = { "X-HOH-Agent-Id": "ui-observer", "X-HOH-Role": "planner" };

statusFilterElement.innerHTML = [
  '<option value="all">All statuses</option>',
  ...statusColumns.map(([value, label]) => `<option value="${value}">${label}</option>`),
].join("");
statusFilterElement.value = state.statusFilter;

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

function initials(value) {
  if (!value) return "–";
  return value.split(/[-\s]/).filter(Boolean).map(part => part[0]).join("").slice(0, 2).toUpperCase();
}

function roleLabel(role) {
  const labels = { planner: "Planner", developer: "Developer", qa: "QA", integrator: "Integrator" };
  return labels[role] || role || "Unassigned";
}

function workflowRole(ticket) {
  if (["BACKLOG", "BLOCKED"].includes(ticket.status)) return "planner";
  if (["QA_READY", "QA_IN_PROGRESS"].includes(ticket.status)) return "qa";
  if (["VERIFIED", "MERGE_READY"].includes(ticket.status)) return "integrator";
  if (["READY", "REWORK", "DEV_IN_PROGRESS"].includes(ticket.status)) return ticket.required_role;
  return null;
}

function ticketOwner(ticket) {
  const owner = ticket.owner || {
    state: ticket.lease_agent_id ? "active" : ticket.status === "BLOCKED" ? "blocked" : ticket.status === "BACKLOG" ? "planning" : "unassigned",
    agent_id: ticket.lease_agent_id || null,
    role: ticket.lease_role || null,
    lease_expires_at: ticket.lease_expires_at || null,
    awaiting_role: ticket.lease_agent_id ? null : workflowRole(ticket),
  };
  const role = owner.role || owner.awaiting_role || null;
  return { ...owner, role, roleLabel: roleLabel(role) };
}

function ownerStatus(owner) {
  if (owner.state === "active") return "Active lease";
  if (owner.state === "expired") return "Lease expired";
  if (owner.state === "blocked") return "Needs Planner decision";
  if (owner.state === "planning") return "Planner review";
  if (!owner.role) return "No active owner";
  return `Awaiting ${owner.roleLabel}`;
}

function ownerTiming(owner) {
  if (!owner.lease_expires_at) return "No exclusive lease";
  const expiry = new Date(owner.lease_expires_at);
  if (Number.isNaN(expiry.getTime())) return "Lease time unavailable";
  const formatted = expiry.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return owner.state === "expired" ? `Expired ${formatted}` : `Until ${formatted}`;
}

function showError(message) {
  errorBanner.textContent = message;
  errorBanner.hidden = false;
}

function clearError() { errorBanner.hidden = true; }

function syncProjectPicker() {
  let option = [...projectPicker.options].find(item => item.value === state.projectId);
  if (!option) {
    option = new Option(state.board.project.name, state.projectId);
    projectPicker.add(option);
  }
  option.textContent = state.board.project.name;
  projectPicker.value = state.projectId;
}

function syncFeatureFilter() {
  const options = ['<option value="all">All features</option>', ...state.board.features.map(
    feature => `<option value="${escapeHtml(feature.feature_id)}">${escapeHtml(feature.title)}</option>`,
  )];
  featureFilterElement.innerHTML = options.join("");
  const known = [...featureFilterElement.options].some(option => option.value === state.featureFilter);
  state.featureFilter = known ? state.featureFilter : "all";
  featureFilterElement.value = state.featureFilter;
}

function updateFilterQueryParams() {
  const params = new URLSearchParams(window.location.search);
  params.set("project", state.projectId);
  if (state.featureFilter === "all") params.delete("feature"); else params.set("feature", state.featureFilter);
  if (state.statusFilter === "all") params.delete("status"); else params.set("status", state.statusFilter);
  window.history.replaceState(null, "", `?${params.toString()}`);
}

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
  syncProjectPicker();
  syncFeatureFilter();
  document.querySelector("#connection-label").textContent = "Live";
  renderBoard();
  renderFeatures();
  renderDispatch();
  renderActivity();
}

function matchesFeatureFilter(ticket) {
  return state.featureFilter === "all" || ticket.feature_id === state.featureFilter;
}

function renderBoard() {
  const alwaysVisible = ["READY", "DEV_IN_PROGRESS", "QA_READY", "REWORK"];
  const visibleColumns = statusColumns.filter(([status]) => {
    if (state.statusFilter !== "all") return status === state.statusFilter;
    return (state.board.columns[status] || []).length || alwaysVisible.includes(status);
  });
  boardElement.innerHTML = visibleColumns.map(([status, label]) => {
    const tickets = (state.board.columns[status] || []).filter(matchesFeatureFilter);
    return `<section class="column" aria-label="${escapeHtml(label)}">
      <div class="column-head">${escapeHtml(label)}<span>${tickets.length}</span></div>
      ${tickets.length ? tickets.map(ticketCard).join("") : '<p class="empty-column meta">No tickets match the current filters.</p>'}
    </section>`;
  }).join("");
  boardElement.querySelectorAll(".ticket").forEach(button => {
    button.addEventListener("click", () => selectTicket(button.dataset.ticketId));
  });
  if (state.selectedTicketId) selectTicket(state.selectedTicketId);
}

function ticketCard(ticket) {
  const owner = ticketOwner(ticket);
  const selected = state.selectedTicketId === ticket.ticket_id ? " selected" : "";
  return `<button class="ticket${selected}" data-ticket-id="${escapeHtml(ticket.ticket_id)}">
    <span class="ticket-top"><span class="ticket-key">${escapeHtml(ticket.ticket_id)}</span><span class="badge">${escapeHtml(ticket.feature_title)}</span></span>
    <span class="ticket-title">${escapeHtml(ticket.title)}</span>
    <span class="ticket-owner ${escapeHtml(owner.state)}" aria-label="Current owner: ${escapeHtml(owner.agent_id || "unassigned")}; ${escapeHtml(ownerStatus(owner))}">
      <span class="avatar">${escapeHtml(initials(owner.agent_id))}</span>
      <span class="owner-copy"><span class="owner-state">${escapeHtml(ownerStatus(owner))}</span><b>${escapeHtml(owner.agent_id || "No active agent")}</b><span class="owner-role">${escapeHtml(owner.roleLabel)} · ${escapeHtml(ownerTiming(owner))}</span></span>
    </span>
    <span class="ticket-foot"><span class="meta">${ticket.evidence_count} evidence · ${ticket.dependency_count} dependencies</span></span>
  </button>`;
}

async function selectTicket(ticketId) {
  const tickets = Object.values(state.board.columns).flat();
  const ticket = tickets.find(item => item.ticket_id === ticketId);
  if (!ticket) return;
  state.selectedTicketId = ticketId;
  boardElement.querySelectorAll(".ticket").forEach(button => button.classList.toggle("selected", button.dataset.ticketId === ticketId));
  renderTicketDetail(ticket, undefined);
  let context = null;
  try {
    const response = await fetch(`/api/v1/tickets/${encodeURIComponent(ticketId)}/context`, { headers: activityHeaders });
    if (!response.ok) throw new Error(`context request failed (${response.status})`);
    context = (await response.json()).data;
  } catch {
    context = { unavailable: "Dependencies, context references and evidence require an authenticated dashboard." };
  }
  if (state.selectedTicketId === ticketId) renderTicketDetail(ticket, context);
}

function renderDependencies(context) {
  if (context === undefined) return `<p class="meta">Loading…</p>`;
  if (context.unavailable) return `<p class="meta">${escapeHtml(context.unavailable)}</p>`;
  const dependencies = context.dependencies || [];
  if (!dependencies.length) return `<p class="meta">No upstream dependencies.</p>`;
  return `<ul>${dependencies.map(dependency => `<li><code>${escapeHtml(dependency.ticket_id)}</code> ${escapeHtml(dependency.title)} · <span class="badge">${escapeHtml(dependency.status)}</span></li>`).join("")}</ul>`;
}

function renderContextReferences(context) {
  if (context === undefined) return `<p class="meta">Loading…</p>`;
  if (context.unavailable) return `<p class="meta">${escapeHtml(context.unavailable)}</p>`;
  const refLine = context.context_ref ? `<p class="meta"><code>${escapeHtml(context.context_ref)}</code></p>` : "";
  const entries = context.context_entries || [];
  if (!entries.length) return `${refLine}<p class="meta">No recorded context entries.</p>`;
  return `${refLine}<ul>${entries.map(entry => `<li><span class="badge">${escapeHtml(entry.entry_type)}</span> ${escapeHtml(entry.actor_id)} (${escapeHtml(roleLabel(entry.actor_role))}) · ${escapeHtml(new Date(entry.created_at).toLocaleString())}</li>`).join("")}</ul>`;
}

function renderLease(owner) {
  return `<div class="detail-stat"><span class="meta">Current owner</span><b>${escapeHtml(owner.agent_id || "Unassigned")}</b><span class="detail-owner">${escapeHtml(owner.roleLabel)} · ${escapeHtml(ownerStatus(owner))} · ${escapeHtml(ownerTiming(owner))}</span></div>`;
}

function renderEvidenceSummary(context, ticket) {
  const stat = `<div class="detail-stat"><span class="meta">Evidence recorded</span><b>${ticket.evidence_count}</b></div>`;
  if (context === undefined) return `${stat}<p class="meta">Loading…</p>`;
  if (context.unavailable) return `${stat}<p class="meta">${escapeHtml(context.unavailable)}</p>`;
  const candidate = context.candidate;
  const testRuns = context.test_runs || [];
  const openFindings = context.known_failures || [];
  const parts = [stat];
  if (candidate) {
    parts.push(`<p class="meta">Candidate <code>${escapeHtml(candidate.commit_sha.slice(0, 8))}</code> submitted by ${escapeHtml(candidate.submitted_by)}</p>`);
  }
  if (testRuns.length) {
    parts.push(`<p class="meta">${testRuns.length} recorded test run${testRuns.length === 1 ? "" : "s"}</p>`);
  }
  parts.push(openFindings.length
    ? `<ul>${openFindings.map(finding => `<li><span class="badge">${escapeHtml(finding.status)}</span> ${escapeHtml(finding.criterion)}</li>`).join("")}</ul>`
    : `<p class="meta">No open findings.</p>`);
  return parts.join("");
}

function renderTicketDetail(ticket, context) {
  const owner = ticketOwner(ticket);
  const relatedCalls = (state.activity?.calls || []).filter(call => {
    const target = call.input_summary?.target || {};
    return target.ticket_id === ticket.ticket_id || target.ticket_ref === `ticket:${ticket.ticket_id}`;
  });
  detailElement.innerHTML = `<p class="eyebrow">${escapeHtml(ticket.ticket_id)}</p>
    <h2>${escapeHtml(ticket.title)}</h2>
    <span class="badge">${escapeHtml(ticket.status)}</span>
    <div class="detail-grid">
      <div class="detail-stat"><span class="meta">Required role</span><b>${escapeHtml(roleLabel(ticket.required_role))}</b></div>
      <div class="detail-stat"><span class="meta">Version</span><b>${ticket.version}</b></div>
      <div class="detail-stat"><span class="meta">Candidate</span><b>${escapeHtml(ticket.candidate_commit ? ticket.candidate_commit.slice(0, 8) : "None")}</b></div>
    </div>
    <h3>Lease</h3>${renderLease(owner)}
    <h3>Objective</h3><p>${escapeHtml(ticket.objective)}</p>
    <h3>Acceptance gate</h3><ul>${ticket.acceptance_criteria.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
    <h3>Preservation constraints</h3><ul>${ticket.preservation_constraints.map(item => `<li>${escapeHtml(item)}</li>`).join("") || "<li>None</li>"}</ul>
    <h3>Write scope</h3><code>${escapeHtml(ticket.file_scope.join(", ") || "Not defined")}</code>
    <h3>Dependencies</h3>${renderDependencies(context)}
    <h3>Context references</h3>${renderContextReferences(context)}
    <h3>Evidence summary</h3>${renderEvidenceSummary(context, ticket)}
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
    "qa.run_project_tests": "queued the frozen candidate test in an isolated worker",
    "qa.get_project_test_run": "polled an isolated QA test run",
    "qa.record_test_run": "recorded a QA test run",
    "qa.submit_verdict": "submitted an evidence-backed QA verdict",
    "integrator.claim_candidate": "claimed a verified candidate for integration",
    "integrator.merge_candidate": "ran the protected merge gate",
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
projectPicker.addEventListener("change", () => {
  state.projectId = projectPicker.value;
  state.selectedTicketId = null;
  state.featureFilter = "all";
  updateFilterQueryParams();
  loadDashboard().catch(error => showError(error.message));
});
featureFilterElement.addEventListener("change", () => {
  state.featureFilter = featureFilterElement.value;
  updateFilterQueryParams();
  renderBoard();
});
statusFilterElement.addEventListener("change", () => {
  state.statusFilter = statusFilterElement.value;
  updateFilterQueryParams();
  renderBoard();
});
document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach(item => item.classList.toggle("active", item === button));
  ["board", "features", "dispatch", "events"].forEach(view => { document.querySelector(`#${view}-view`).hidden = view !== button.dataset.view; });
}));

loadDashboard().catch(error => { document.querySelector("#connection-label").textContent = "Offline"; showError(error.message); });
window.setInterval(() => loadDashboard().catch(() => {}), 10000);
