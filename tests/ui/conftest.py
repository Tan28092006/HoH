"""Isolated candidate app, fixture database and retained browser evidence."""
from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture(scope="module")
def qa_app(tmp_path_factory):
    import uvicorn
    from hoh_control_plane.api import create_app
    from hoh_control_plane.domain import Actor, Role
    import hoh_control_plane

    source = Path(__file__).resolve().parents[2]
    package = Path(hoh_control_plane.__file__).resolve()
    assert package.is_relative_to(source / "src"), "QA imported an ambient checkout"
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    fixture_dir = tmp_path_factory.mktemp("qa-ui")
    with pytest.MonkeyPatch.context() as environment:
        for key in ("HOH_DATABASE_URL", "HOH_ARTIFACT_ROOT", "HOH_ADMIN_TOKEN", "HOH_AGENT_TOKENS_JSON"):
            environment.delenv(key, raising=False)
        app = create_app(fixture_dir / "fixture.db", local_mode=True, seed_demo=True)
    planner = Actor("fixture-planner", Role.PLANNER, frozenset({"*"}))
    service = app.state.service
    service.create_project(planner, "ui-empty", "Empty project", "UI isolation fixture", "main")
    service.create_feature(planner, "demo", "ui-backlog-feature", "UI backlog", "Filter fixture")
    service.create_ticket(planner, "ui-backlog-feature", "UI-BACKLOG", "Đơn hàng — pending review", "Visible backlog",
                          Role.DEVELOPER, ["Visible to operators"], [], ["ui/**"], ready=False)
    dirty = bool(subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip())
    identity = {"candidate_commit": sha, "source_tree_dirty": dirty,
                "source_root": str(source), "fixture_database": str(fixture_dir / "fixture.db")}

    @app.get("/__qa_identity")
    def candidate_identity():
        return identity

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "Candidate UI did not start"
    try:
        yield {"url": url, **identity}
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "Candidate UI did not stop"


@pytest.fixture
def ui_browser(request):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=request.config.getoption("--ui-channel"))
        yield browser
        browser.close()


@pytest.fixture
def browser_case(request, qa_app, ui_browser, tmp_path):
    configured = request.config.getoption("--ui-artifacts")
    root = Path(configured).resolve() if configured else tmp_path / "browser-evidence"
    if root.is_relative_to(Path(qa_app["source_root"])):
        pytest.fail("Browser evidence must be outside the frozen candidate worktree")
    target = root / (request.node.name.replace("[", "-").replace("]", "") + "-" + uuid4().hex[:8])
    target.mkdir(parents=True)
    context = ui_browser.new_context()
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()
    errors, requests = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda req: requests.append({"method": req.method, "url": req.url}))
    page.on("response", lambda response: errors.append(f"HTTP {response.status}: {response.url}") if response.status >= 400 else None)
    try:
        yield page, errors, requests
    finally:
        page.screenshot(path=str(target / "screen.png"), full_page=True)
        context.tracing.stop(path=str(target / "trace.zip"))
        manifest = {**qa_app, "scenario": request.node.nodeid, "browser": ui_browser.version,
                    "viewport": page.viewport_size, "errors": errors, "requests": requests,
                    "artifacts": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in target.iterdir()}}
        (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"UI evidence: {target}")
        context.close()
