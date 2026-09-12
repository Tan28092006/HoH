import json
import re
from pathlib import Path
import socket
import subprocess
import threading
import time

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture(scope='module')
def qa_app(tmp_path_factory):
    import uvicorn
    from hoh_control_plane.desktop_control import create_desktop_app
    from hoh_control_plane.domain import Actor, Role
    source = Path(__file__).resolve().parents[2]
    root = tmp_path_factory.mktemp('desktop-ui')
    (root / 'config').mkdir()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    (root / 'config/settings.json').write_text(json.dumps({'database_path': str(root / 'data/db.sqlite'),
        'ui_port': port, 'project_id': 'alpha', 'installation_id': 'ui-fixture'}))
    app = create_desktop_app(root)
    app.state.service.create_project(Actor('fixture', Role.ADMIN, frozenset({'*'})), 'alpha', 'Alpha', '', 'main')
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.05)
    assert server.started
    try:
        yield {'url': f'http://127.0.0.1:{port}', 'source_root': str(source), 'root': str(root),
            'candidate_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
            'source_tree_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip())}
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()


def test_create_project_and_get_four_bound_prompts(qa_app, browser_case):
    from playwright.sync_api import expect
    page, errors, _ = browser_case
    page.goto(qa_app['url'] + '/?project=alpha')
    page.get_by_role('button', name='Thêm dự án', exact=True).click()
    page.get_by_label('Tên dự án').fill('Cửa hàng mới')
    page.get_by_label('Project ID', exact=True).fill('new-shop')
    page.get_by_label('Mô tả dự án').fill('Quản lý đơn hàng')
    page.get_by_role('button', name='Tạo dự án', exact=True).click()
    expect(page).to_have_url(qa_app['url'] + '/?project=new-shop')
    expect(page.locator('#project-picker')).to_have_value('new-shop')
    page.reload()
    expect(page.locator('#project-picker').locator('option')).to_have_count(2)
    page.get_by_role('button', name='Prompt cho agent', exact=True).click()
    page.get_by_label('Instance của chat').fill('codex-dev-2')
    page.get_by_role('button', name='Áp dụng instance', exact=True).click()
    for role in ['Planner', 'Developer', 'QA', 'Integrator']:
        page.get_by_role('button', name=role, exact=True).click()
        expect(page.get_by_label('Prompt cài đặt')).to_have_value(re.compile('new-shop'))
        expect(page.get_by_label('Prompt cài đặt')).to_have_value(re.compile('session.bootstrap'))
        expect(page.get_by_label('Prompt cài đặt')).to_have_value(re.compile('--instance codex-dev-2'))
        expect(page.get_by_label('Prompt cài đặt')).to_have_value(re.compile('work.wait_for_event'))
    for width in [1440, 768, 375]:
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors


def test_agent_wait_monitor_is_visible_without_spawning_agents(qa_app, browser_case):
    from playwright.sync_api import expect
    page, errors, _ = browser_case
    page.goto(qa_app['url'] + '/?project=alpha')
    page.get_by_role('button', name='Phiên agent', exact=True).click()
    expect(page.get_by_text('Chưa có phiên chờ sự kiện trên project này.', exact=True)).to_be_visible()
    expect(page.get_by_text('Chat cần mở và đang chờ MCP. Bấm Stop hoặc đóng chat sẽ ngắt chờ.', exact=True)).to_be_visible()
    assert not errors


def test_shutdown_confirmation_does_not_stop_on_cancel(qa_app, browser_case):
    from playwright.sync_api import expect
    page, errors, _ = browser_case
    page.goto(qa_app['url'] + '/?project=alpha')
    page.get_by_role('button', name='Tắt HoH', exact=True).click()
    page.get_by_role('button', name='Tiếp tục sử dụng', exact=True).click()
    assert not (Path(qa_app['root']) / 'config/shutdown.request').exists()
    page.get_by_role('button', name='Tắt HoH', exact=True).click()
    page.get_by_role('button', name='Xác nhận tắt', exact=True).click()
    expect(page.get_by_role('status').filter(has_text='Đang tắt')).to_be_visible()
    assert (Path(qa_app['root']) / 'config/shutdown.request').exists()
    assert not errors
