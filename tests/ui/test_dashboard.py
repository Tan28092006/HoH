import pytest

pytestmark = pytest.mark.ui


@pytest.mark.parametrize("width", [375, 768])
def test_narrow_keyboard_selection_context_journey(qa_app, browser_case, width):
    import re
    from playwright.sync_api import expect
    page, errors, requests = browser_case
    page.set_viewport_size({"width": width, "height": 812})
    page.goto(qa_app["url"] + "/?project=demo&status=BACKLOG")
    backlog = page.locator('[data-ticket-id="UI-BACKLOG"]')
    backlog.focus()
    expect(backlog).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#detail")).to_contain_text("Visible backlog")
    reference = page.locator("#detail li").filter(has_text="known_failure")
    expect(reference).to_contain_text("Planner")
    expect(reference).not_to_contain_text("Invalid Date")
    quoted = page.get_by_role("button", name=re.compile('UI-"QUOTED'))
    quoted.focus()
    page.keyboard.press("Space")
    expect(page.locator("#detail")).to_contain_text("Quoted detail")
    backlog.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#detail")).to_contain_text("Visible backlog")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors and all(r["method"] == "GET" for r in requests)


def test_hostile_quoted_id_is_literal_not_markup(qa_app, browser_case):
    import re
    from playwright.sync_api import expect
    page, errors, _ = browser_case
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    page.goto(qa_app["url"] + "/?project=demo&status=BACKLOG")
    hostile = page.get_by_role("button", name=re.compile("Hostile quoted ID"))
    expect(hostile).to_have_attribute("data-ticket-id", 'UI-"><img src=x onerror=alert(1)>')
    hostile.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#detail")).to_contain_text("Literal hostile detail")
    expect(page.locator('#board img, #detail img, #board [onerror], #detail [onerror]')).to_have_count(0)
    page.locator('[data-ticket-id="UI-BACKLOG"]').click()
    expect(page.locator("#detail")).to_contain_text("Visible backlog")
    assert dialogs == [] and errors == []


@pytest.mark.parametrize("timestamp", [None, "not-a-timestamp"])
def test_context_reference_ignores_absent_or_malformed_timestamp(qa_app, browser_case, timestamp):
    from playwright.sync_api import expect
    page, errors, _ = browser_case
    def projected(route):
        response = route.fetch()
        payload = response.json()
        entries = payload["data"]["context_entries"]
        assert entries
        for entry in entries:
            entry.pop("created_at", None)
            if timestamp is not None:
                entry["created_at"] = timestamp
        route.fulfill(response=response, json=payload)
    page.route("**/api/v1/tickets/UI-BACKLOG/context", projected)
    page.goto(qa_app["url"] + "/?project=demo&status=BACKLOG")
    page.locator('[data-ticket-id="UI-BACKLOG"]').click()
    reference = page.locator("#detail li").filter(has_text="known_failure")
    expect(reference).to_contain_text("Planner")
    expect(reference).to_contain_text("ctx:entry:")
    expect(reference).not_to_contain_text("Invalid Date")
    expect(reference).not_to_contain_text("Unassigned")
    assert not errors


def test_quoted_ticket_id_round_trips_through_selection(qa_app, browser_case):
    import re
    from playwright.sync_api import expect
    page, errors, _ = browser_case
    page.goto(qa_app["url"] + "/?project=demo&status=BACKLOG")
    quoted = page.get_by_role("button", name=re.compile('UI-"QUOTED'))
    expect(quoted).to_have_attribute("data-ticket-id", 'UI-"QUOTED')
    quoted.click()
    expect(page.locator("#detail")).to_contain_text("Quoted detail")
    page.locator('[data-ticket-id="UI-BACKLOG"]').click()
    expect(page.locator("#detail")).to_contain_text("Visible backlog")
    quoted.click()
    expect(page.locator("#detail")).to_contain_text("Quoted detail")
    assert not errors


def test_context_reference_uses_projected_packet_fields(qa_app, browser_case):
    from playwright.sync_api import expect
    page, errors, _ = browser_case
    page.goto(qa_app["url"] + "/?project=demo&status=BACKLOG")
    page.locator('[data-ticket-id="UI-BACKLOG"]').click()
    expect(page.locator("#detail")).to_contain_text("known_failure")
    reference = page.locator("#detail li").filter(has_text="known_failure")
    expect(reference).to_contain_text("Planner")
    expect(reference).not_to_contain_text("Invalid Date")
    expect(reference).not_to_contain_text("Unassigned")
    assert not errors


@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 900}, {"width": 768, "height": 900}, {"width": 375, "height": 812}])
def test_operator_can_follow_ticket_and_filter_state(qa_app, browser_case, viewport):
    from playwright.sync_api import expect
    page, errors, requests = browser_case
    page.set_viewport_size(viewport)
    page.goto(qa_app["url"] + "/?project=demo&status=BACKLOG")
    expect(page.get_by_role("combobox", name="Filter by status")).to_have_value("BACKLOG")
    expect(page.locator('[data-ticket-id="UI-BACKLOG"]')).to_be_visible()
    page.locator('[data-ticket-id="UI-BACKLOG"]').click()
    expect(page.locator("#detail")).to_contain_text("Visible backlog")
    page.get_by_role("combobox", name="Filter by feature").select_option("ui-backlog-feature")
    page.reload()
    expect(page.get_by_role("combobox", name="Filter by feature")).to_have_value("ui-backlog-feature")
    expect(page.locator('[data-ticket-id="UI-BACKLOG"]')).to_be_visible()
    for label, selector in [("Features", "#features-view"), ("Dispatch", "#dispatch-view"), ("Agent activity", "#events-view"), ("Board", "#board-view")]:
        page.get_by_role("button", name=label, exact=True).click()
        expect(page.locator(selector)).to_be_visible()
    page.get_by_role("combobox", name="Filter by status").focus()
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement !== document.body")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Page overflows viewport"
    assert not errors
    assert all(request["method"] == "GET" for request in requests), "Read-only board mutated state"


def test_background_refresh_preserves_ticket_keyboard_focus(qa_app, browser_case):
    from playwright.sync_api import expect
    page, errors, _ = browser_case
    page.goto(qa_app["url"] + "/?project=demo&status=BACKLOG")
    ticket = page.locator('[data-ticket-id="UI-BACKLOG"]')
    expect(ticket).to_be_visible()
    baseline = page.request.get(qa_app["url"] + "/api/v1/projects/demo/board").json()
    baseline["data"]["columns"]["BACKLOG"][0]["title"] = "Updated while using the keyboard"
    page.route("**/api/v1/projects/demo/board", lambda route: route.fulfill(json=baseline))
    ticket.focus()
    expect(ticket).to_be_focused()
    # Trigger the same refresh without moving focus to the Refresh button.
    page.locator("#refresh-button").evaluate("button => button.click()")
    expect(ticket).to_contain_text("Updated while using the keyboard")
    expect(ticket).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#detail")).to_contain_text("Visible backlog")
    # A status transition may remove the focused ticket from this filtered view.
    baseline["data"]["columns"]["BACKLOG"] = []
    page.locator("#refresh-button").evaluate("button => button.click()")
    expect(ticket).to_have_count(0)
    expect(page.get_by_role("combobox", name="Filter by status")).to_be_focused()
    assert not errors


def test_old_response_cannot_replace_a_newer_snapshot(qa_app, browser_case):
    from copy import deepcopy
    from playwright.sync_api import expect
    page, _, _ = browser_case
    page.goto(qa_app["url"] + "/?project=demo")
    expect(page.locator("#board .ticket").first).to_be_visible()
    baseline = page.request.get(qa_app["url"] + "/api/v1/projects/demo/board").json()
    held = []

    def delay_first(route):
        # Fetch fixture data before routing: route.fetch() can re-enter this
        # handler while another refresh/SSE request is still being processed.
        data = deepcopy(baseline)
        if not held:
            data["data"]["project"]["description"] = "STALE RESPONSE"
            held.append((route, data))
        else:
            data["data"]["project"]["description"] = "CURRENT RESPONSE"
            route.fulfill(json=data)

    page.route("**/api/v1/projects/demo/board", delay_first)
    page.get_by_role("button", name="Refresh", exact=True).click()
    page.wait_for_timeout(50)
    assert len(held) == 1, "The earlier request must be held before the next refresh"
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#project-description")).to_have_text("CURRENT RESPONSE")
    with page.expect_response("**/api/v1/projects/demo/board") as delayed:
        held[0][0].fulfill(json=held[0][1])
    delayed.value.finished()
    # Keep the handler until context teardown so background refreshes cannot
    # race unroute() while a callback is fulfilling a response.
    # A further browser task runs after the fulfilled fetch's continuation.
    page.wait_for_timeout(100)
    expect(page.locator("#project-description")).to_have_text("CURRENT RESPONSE")


def test_empty_project_and_error_are_observable(qa_app, browser_case):
    from playwright.sync_api import expect
    page, _, _ = browser_case
    page.goto(qa_app["url"] + "/?project=ui-empty")
    expect(page.locator("#project-description")).to_have_text("UI isolation fixture")
    expect(page.locator("#board .ticket")).to_have_count(0)
    page.route("**/api/v1/projects/ui-empty/board", lambda route: route.fulfill(status=503, json={"error": "fixture outage"}))
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("503")
