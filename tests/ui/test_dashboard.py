import pytest

pytestmark = pytest.mark.ui


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


def test_old_response_cannot_replace_a_newer_snapshot(qa_app, browser_case):
    from playwright.sync_api import expect
    page, _, _ = browser_case
    page.goto(qa_app["url"] + "/?project=demo")
    expect(page.locator("#board .ticket").first).to_be_visible()
    held = []

    def delay_first(route):
        response = route.fetch()
        data = response.json()
        if not held:
            data["data"]["project"]["description"] = "STALE RESPONSE"
            held.append((route, data))
        else:
            data["data"]["project"]["description"] = "CURRENT RESPONSE"
            route.fulfill(json=data)

    page.route("**/api/v1/projects/demo/board", delay_first)
    page.get_by_role("button", name="Refresh", exact=True).click()
    page.wait_for_function("true")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#project-description")).to_have_text("CURRENT RESPONSE")
    held[0][0].fulfill(json=held[0][1])
    page.unroute("**/api/v1/projects/demo/board", delay_first)
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
