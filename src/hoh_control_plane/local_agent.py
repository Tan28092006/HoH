"""Project-scoped stdio profiles for agents running on the desktop host."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from hoh_control_plane.domain import Actor, Role, NotFoundError

ROLES = ('planner', 'developer', 'qa', 'integrator')


def profile_actor(project, role, instance):
    if role not in ROLES or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', project) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', instance):
        raise ValueError('Invalid project, role or instance')
    digest = hashlib.sha256((project + ':' + role + ':' + instance).encode()).hexdigest()[:16]
    return Actor(f'local-{role}-{digest}', Role(role), frozenset({project}))


def checked_project(root, project):
    config = json.loads((root / 'config/settings.json').read_text(encoding='utf-8'))
    database = Path(config['database_path']).resolve()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT name FROM projects WHERE project_id = ?', (project,)).fetchone()
    if row is None:
        raise NotFoundError('Project does not exist on this HoH host')
    return config


def install_profile(root, project, role, instance, client, home, upgrade_skills=False, transport='stdio'):
    from hoh_control_plane.agent_kit import build_kit
    from hoh_control_plane.agent_assets.install import install
    config = checked_project(root, project)
    if transport not in {'stdio', 'shared-http'}:
        raise ValueError('Unknown local MCP transport')
    actor = profile_actor(project, role, instance)
    server = 'hoh-' + actor.agent_id
    command = str(root / '.venv/Scripts/python.exe')
    arguments = ['-m', 'hoh_control_plane.local_agent', 'serve', '--root', str(root),
                 '--project', project, '--role', role, '--instance', instance]
    codex = {'command': command, 'args': arguments, 'tool_timeout_sec': 86400}
    claude = {'type': 'stdio', 'command': command, 'args': arguments, 'timeout': 86400000}
    previous = {'codex': codex, 'claude-code': claude}
    if transport == 'shared-http':
        from hoh_control_plane.local_credentials import register_local_credential
        token = register_local_credential(root, actor)
        url = f"http://127.0.0.1:{int(config.get('mcp_port', 8931))}/mcp"
        headers = {'Authorization': 'Bearer ' + token}
        codex = {'url': url, 'http_headers': headers, 'tool_timeout_sec': 86400}
        claude = {'type': 'http', 'url': url, 'headers': headers, 'timeout': 86400000}
    with tempfile.TemporaryDirectory(prefix='hoh-profile-') as temp:
        kit = Path(temp) / 'kit'
        build_kit(kit, role, 'http://127.0.0.1:8931/mcp')
        if transport == 'shared-http':
            codex_text = (f'[mcp_servers.{server}]\nurl = {json.dumps(url)}\ntool_timeout_sec = 86400\n'
                          f'http_headers = {{ Authorization = {json.dumps(headers["Authorization"])} }}\n')
        else:
            codex_text = f'[mcp_servers.{server}]\ncommand = {json.dumps(command)}\nargs = {json.dumps(arguments)}\ntool_timeout_sec = 86400\n'
        (kit / 'codex.toml').write_text(codex_text, encoding='utf-8')
        (kit / 'mcp.json').write_text(json.dumps({'mcpServers': {server: claude}}), encoding='utf-8')
        manifest = json.loads((kit / 'manifest.json').read_text(encoding='utf-8'))
        manifest['server_name'], manifest['codex'] = server, codex
        if transport == 'shared-http':
            manifest['upgrade_from'] = previous
        manifest['files'] = {p.relative_to(kit).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in kit.rglob('*') if p.is_file() and p.name != 'manifest.json'}
        (kit / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        moved = []
        try:
            if upgrade_skills:
                skill_root = home / ('.codex/skills' if client == 'codex' else '.claude/skills')
                for source in (kit / 'skills').iterdir():
                    destination = skill_root / source.name
                    if not destination.exists():
                        continue
                    if not destination.resolve().is_relative_to(skill_root.resolve()) or destination.is_symlink() or destination.is_junction():
                        raise ValueError('Refusing to replace a linked skill directory')
                    contents = lambda path: {p.relative_to(path): p.read_bytes() for p in path.rglob('*') if p.is_file()}
                    if contents(source) != contents(destination):
                        backup_root = home / '.hoh-skill-backups'
                        backup_root.mkdir(exist_ok=True)
                        backup = Path(tempfile.mkdtemp(prefix=source.name + '-', dir=backup_root)) / source.name
                        shutil.move(str(destination), backup)
                        moved.append((destination, backup))
            install(kit, home, client)
        except BaseException:
            for destination, backup in reversed(moved):
                if not destination.exists():
                    shutil.move(str(backup), destination)
            raise
    return {'server_name': server, 'agent_id': actor.agent_id, 'role': role, 'project_id': project, 'transport': transport,
            'skill_backups': [str(backup) for _, backup in moved]}


def make_prompts(root, project, instance='primary'):
    root = str(root)
    role_work = {
        'planner': 'Đọc spec và context; nếu repository/test command chưa cấu hình, xác minh đường dẫn Git, default branch, workspace model và lệnh test với người dùng trước khi configure_repository/configure_test_command. Khai báo verification profile của project và validation contract claim→scenario→evidence cho từng criterion trước khi READY. Không tự coi thư mục chat là repo dự án. Không implement, QA hay merge.',
        'developer': 'Nhận một ticket Developer có lease hợp lệ. Đọc validation contract, tạo public runtime records cần cho QA nhưng không tự chứng nhận acceptance. Với colocated, chỉ sửa worktree do dev.prepare_worktree trả về; với agent_workspace, dùng clone/worktree riêng và bảo đảm SHA được chuyển tới service repo. Giữ dirty work, kiểm tra baseline, branch và scope. Submit candidate một lần rồi poll job đến terminal; không tự QA hoặc merge.',
        'qa': 'Nhận candidate đã đóng băng bằng QA lease. Thực hiện claim→observe→assess theo từng validation contract; kiểm tra đúng candidate SHA, source import và capability môi trường. Chạy command QA đã đăng ký, poll test_run_id; dùng UI/browser/runtime trace/media khi contract yêu cầu. Evidence để ngoài candidate, không sửa code/test của candidate. Chỉ PASS khi đủ mọi scenario và evidence type.',
        'integrator': 'Chỉ nhận candidate đã QA-verified/merge-ready, kiểm tra đúng SHA, base, validation-contract evidence bundle và hold. Dùng queued integrator.merge_candidate một lần rồi poll merge_job_id; xác minh trạng thái DONE và mainline SHA. Nếu drift/conflict hoặc SHA đổi, chuyển về luồng rework/QA; không force-push, merge tay hay sửa DB.'}
    prompts = {}
    for role in ROLES:
        actor = profile_actor(project, role, instance)
        prompts[role] = f'''Thiết lập và làm việc với HoH trên máy Windows này.
Vai trò: {role}. Project ID bắt buộc: {project}. Instance: {instance}.
Tôi cho phép cài/cập nhật đúng skill HoH base + {role} và thêm MCP profile này; sao lưu skill HoH cũ, giữ nguyên mọi cấu hình khác.

1. Xác định client là Codex hay Claude Code. Dùng shell PowerShell chạy:
& '{root}\\.venv\\Scripts\\python.exe' -I -m hoh_control_plane.local_agent setup --root '{root}' --project '{project}' --role {role} --instance {instance} --client codex --upgrade-skills --transport shared-http
Nếu là Claude Code, thay giá trị --client bằng claude-code. Không cài cả hai client nếu không cần.
2. Load $hoh-control-plane và $hoh-{role}. MCP server cần dùng là hoh-{actor.agent_id}. Nếu công cụ mới chưa xuất hiện, báo cần reload MCP/restart client rồi tiếp tục; không giả vờ đã kết nối hoặc dùng server role/project khác.
3. Gọi session.bootstrap(project_id="{project}") trên đúng MCP đó. Xác minh identity.role="{role}", identity.agent_id="{actor.agent_id}", identity.project_ids chỉ chứa "{project}" và selected_project_id="{project}". Sai bất kỳ mục nào thì dừng trước khi ghi/claim.
4. Resume lease/job hiện có trước khi nhận việc khác. Đăng ký đúng role/skills, capacity=1, kiểm tra inbox và lọc discovery theo project này. Không tự tạo lại project đã có. Nếu chính chat này đang tiếp tục sau restart/Stop/timeout thì giữ instance {instance}: lấy wait_id trong event_wait của bootstrap và truyền resume_wait_id khi gọi work.wait_for_event, kèm job_kind/job_id nếu đang nối lại job. Chỉ lệnh chờ cũ bị thay; lease, job và worktree giữ nguyên. Nếu wait đã đổi, bootstrap lại một lần; xung đột lặp lại thì báo rõ thay vì tranh giành. Khi nhận status=superseded, dừng yêu cầu cũ và không tự retry. Không kết luận có nhiều chat làm việc chỉ từ số process MCP. Chỉ một chat khác cùng role đang thực sự làm việc song song mới cần instance riêng.
5. {role_work[role]}
6. Chỉ dùng tool thật được server công bố, giữ lease khi đang làm việc, lưu checkpoint/handoff bền vững. Không truy cập DB trực tiếp, không sửa worktree của agent khác, không đưa token vào chat. Kết thúc thiết lập bằng cách báo client, MCP server, role, project ID, repository/workspace model và việc sẽ làm tiếp. Không suy diễn MCP có thể tự đánh thức một chat đã dừng.

7. Chế độ GUI nhận sự kiện, không cron: khi hết việc sẵn sàng, thông báo ngắn rồi gọi work.wait_for_event(project_id="{project}", timeout_seconds=43200) và để chat chờ tool. Không kết thúc lượt chỉ vì inbox rỗng, không lặp polling bằng lượt model. Khi có event, resolve context hiện tại và claim theo đúng role; event không cấp lease. Giữ event_id, truyền ack_event_id ở lần wait tiếp theo sau khi đã xử lý hoặc xác định event hết hiệu lực. Khi queue job provision/submission/qa/merge, dùng work.wait_for_event với job_kind và job_id tương ứng, sau đó gọi poll_tool trong kết quả đúng một lần để lấy trạng thái. HoH duy trì lease hợp lệ của job trong lúc tool đang chờ. Nếu cần bạn trả lời thì báo rõ và dừng; không giấu câu hỏi trong một lần chờ 12 giờ. Nếu timeout/mất kết nối, kiểm tra lại host và bootstrap; không tự tạo cron. Đóng chat, bấm Stop, sleep máy hoặc tắt HoH sẽ ngắt chờ. Cách này giữ chính chat Codex app/Claude extension; không tạo phiên CLI thay thế.

Cấu hình này dùng MCP HTTP chung tại loopback, không tạo thêm Python server theo từng chat. Setup tự cấp credential riêng cho identity/project và lưu trong cấu hình client; không chép token vào chat. Mở HoH bằng icon Desktop trước khi kết nối; profile HTTP không tự bật host đã tắt. Sau khi chuyển từ stdio, reload MCP/restart client một lần. Máy khác cần bộ cấu hình HTTP riêng và đường mạng đã được cấp quyền.'''
    return prompts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'serve'])
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--project', required=True)
    parser.add_argument('--role', required=True, choices=ROLES)
    parser.add_argument('--instance', default='primary')
    parser.add_argument('--client', choices=['codex', 'claude-code'], default='codex')
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--upgrade-skills', action='store_true')
    parser.add_argument('--transport', choices=['stdio', 'shared-http'], default='shared-http')
    args = parser.parse_args()
    root = args.root.resolve()
    config = checked_project(root, args.project)
    actor = profile_actor(args.project, args.role, args.instance)
    if args.action == 'setup':
        print(json.dumps(install_profile(root, args.project, args.role, args.instance, args.client, args.home, args.upgrade_skills, args.transport)))
        return
    if any((root / 'config' / name).exists() for name in ('stop.request', 'shutdown.request')):
        raise SystemExit('HoH was stopped by the operator. Open the HoH desktop icon before reconnecting.')
    os.environ.pop('HOH_DATABASE_URL', None)
    os.environ.pop('PYTHONPATH', None)
    os.environ.pop('PYTHONHOME', None)
    os.environ.update(HOH_DATABASE_PATH=config['database_path'], HOH_WORKER_MODE='supervised',
                      HOH_MCP_TRANSPORT='stdio', HOH_AGENT_ID=actor.agent_id,
                      HOH_AGENT_ROLE=args.role, HOH_PROJECT_IDS=args.project)
    # Start the supervised host while reserving stdout for MCP protocol frames.
    subprocess.run([sys.executable, str(root / 'hoh_desktop.py'), '--no-browser'],
                   stdout=subprocess.DEVNULL, check=True, creationflags=0x08000000 if os.name == 'nt' else 0)
    def watch_stop():
        while True:
            if (root / 'config/stop.request').exists():
                os._exit(0)
            time.sleep(1)
    threading.Thread(target=watch_stop, daemon=True).start()
    from hoh_control_plane.mcp_server import main as serve
    serve()


if __name__ == '__main__':
    main()
