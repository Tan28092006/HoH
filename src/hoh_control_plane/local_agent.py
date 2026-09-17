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


def profile_actor(project, role, instance, salt=None):
    """Derive a profile identity.

    With a salt (the installation id) the identity is unique to this HoH
    installation, so two machines that share a Git remote and happen to use the
    same project, role and instance names never produce the same branch or
    worktree names. Without a salt the legacy derivation is kept, because
    identities registered before the salt existed must keep resolving.
    """
    if role not in ROLES or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', project) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', instance):
        raise ValueError('Invalid project, role or instance')
    if salt:
        digest = hashlib.sha256((str(salt) + ':' + project + ':' + role + ':' + instance).encode()).hexdigest()[:8]
    else:
        digest = hashlib.sha256((project + ':' + role + ':' + instance).encode()).hexdigest()[:16]
    return Actor(f'local-{role}-{digest}', Role(role), frozenset({project}))


def installed_profile_actor(root, config, project, role, instance):
    """The identity a profile has on this installation.

    A profile registered before identities were salted keeps its legacy id, so
    an existing chat's credential, leases and worktrees stay its own; a new
    profile is salted with the installation id.
    """
    from hoh_control_plane.auth import credentials_from_environment
    legacy = profile_actor(project, role, instance)
    path = Path(root) / 'config/agent-credentials.json'
    if path.exists():
        for credential in credentials_from_environment(path.read_text(encoding='utf-8')):
            if credential.actor == legacy:
                return legacy
    database = (config or {}).get('database_path')
    if database:
        with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            if db.execute('SELECT 1 FROM agent_presence WHERE agent_id=?', (legacy.agent_id,)).fetchone():
                return legacy
    salt = (config or {}).get('installation_id')
    return profile_actor(project, role, instance, salt) if salt else legacy


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
    actor = installed_profile_actor(root, config, project, role, instance)
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


def _make_prompts_0_3_18_legacy(root, project, instance='primary'):
    root_path = Path(root)
    # Forward slashes are accepted by PowerShell on Windows and survive being
    # copied through Markdown/LLM clients.  In particular, avoid ``\.venv``:
    # some clients normalize ``\.`` as an escape and silently turn
    # ``HoH-service\.venv`` into the nonexistent ``HoH-service.venv``.
    root = root_path.as_posix()
    python = (root_path / '.venv' / 'Scripts' / 'python.exe').as_posix()
    role_work = {
        'planner': 'Planner là control loop mạnh nhất về định hướng và lấy commit làm nhịp iteration. Trước mỗi wait: (1) planner.sync_repository fetch toàn bộ upstream refs; (2) với từng commit trong observed_commits, đọc diff code/docs/spec và đối chiếu artifact thật; (3) gọi planner.list_repository_reconciliations, audit mọi ticket mở được chụp trong ticket_versions và phân loại keep/revise/cancel/supersede/complete/rework; (4) nếu ticket lỗi thời ở bất kỳ phase nào, dùng planner.retire_ticket_for_reconciliation để thu hồi quyền chạy ngay nhưng giữ worktree/candidate/evidence; poll request nếu merge đang RUNNING và nếu kết quả LANDED thì đánh giá revert thay vì giả vờ cancel. Chỉ dùng stop_ticket khi ticket vẫn hợp lệ nhưng cần revise. Tạo ticket fix/adoption mới khi code mới tạo gap; commit ngoài gate chỉ được DONE sau khi planner.record_external_landing xác minh Git; (5) xử lý Planner jobs từ dev/QA/integrator: đọc đúng evidence refs, check Git diff và docs/spec liên quan, rồi retire/revise hoặc dùng planner.resume_ticket_after_failure để đóng job và trả ticket về đúng role; (6) xử lý capability_review_required: missing_declaration là trang bị QA chưa xác minh nên phải provision/route rồi yêu cầu QA re-register; missing_approval chỉ được duyệt sau public preflight evidence trên đúng machine_id; không duyệt từ self-report; (7) gọi planner.resolve_repository_reconciliation với quyết định cho đúng mọi ticket cũ và new_ticket_ids. Runtime chặn READY, claim, Developer submit và merge cho đến khi mọi observation được resolve theo thứ tự. Sau đó mới chọn tối đa ba increment quan sát được và wait. Không git pull/checkout/merge/rebase shared integration checkout. Branch không liên quan vẫn phải có quyết định keep kèm lý do; branch gắn ticket/team phải đọc diff chi tiết. Thực hiện vòng này đầu session, sau mỗi event, trước READY, sau delivery_completed và sau mọi repository_changed. Remote commit không tự là QA evidence. Không tự coi thư mục chat là repo dự án. Không implement, QA hay merge.',
        'developer': 'Nhận một ticket Developer có lease hợp lệ. Đọc validation contract, tạo public runtime records cần cho QA nhưng không tự chứng nhận acceptance. Với colocated, chỉ sửa worktree do dev.prepare_worktree trả về; với agent_workspace, dùng clone/worktree riêng và bảo đảm SHA được chuyển tới service repo. Giữ dirty work, kiểm tra baseline, branch và scope. Ngay trước submit phải gọi ticket.get_context lại và kiểm tra planning_barrier/retirement. Nếu repository_reconciliation_required, ngừng thay đổi mới, giữ worktree/commit hiện tại và lease; không submit hoặc tự rebase cho tới khi Planner giữ/revise/stop ticket. Nếu Planner thu hồi lease hay packet có retirement thì dừng ngay, không submit từ worktree cũ. Snapshot trước work.claim đã stale vì claim tăng ticket version. Nếu submit vẫn bị context_stale, refresh packet và chỉ retry đúng một lần khi server xác nhận chưa có job/candidate và mọi input không đổi. Submit đúng một commit thường cho một iteration; parent phải là frozen base hoặc candidate trước đã được QA/Planner đánh giá trong rework. Runtime từ chối merge commit và multi-commit range. Thành công thì poll job đến terminal; không tự QA hoặc merge. Trước wait, lỗi source/contract đang giữ lease dùng dev.report_blocker; provision/submission FAILED hoặc lỗi durable khác chưa có planner_job dùng work.report_failure đúng một lần. Không báo empty inbox, context_stale, state/lease conflict hay transient đầu tiên.',
        'qa': 'Nhận candidate đã đóng băng bằng QA lease. Chỉ khai execution_capabilities đã tự kiểm tra; việc claim còn cần Planner/operator duyệt profile từ public preflight evidence và QA không tự duyệt. Luôn đọc capability_blockers trong work.inbox và capability_state trong work.discover trước khi nói không có candidate. Nếu missing_declaration, kiểm tra tool/adapter thật rồi re-register đúng một lần với tập đã xác minh; phần còn thiếu là infrastructure để Planner provision/route. Nếu chỉ missing_approval, giữ nguyên candidate và chờ Planner duyệt evidence trên đúng machine_id. Thực hiện claim→observe→assess theo từng validation contract; kiểm tra đúng candidate SHA, source import và capability môi trường. Chạy command QA đã đăng ký, poll test_run_id; dùng UI/browser/runtime trace/media khi contract yêu cầu. Evidence để ngoài candidate, không sửa code/test của candidate. Chỉ PASS khi đủ mọi scenario và evidence type. QA fail và lỗi runner hạ tầng đã tự mở Planner job; không báo lặp từng assertion. Lỗi tool/worker durable khác chưa có planner_job thì gọi work.report_failure đúng một lần trước wait. QA fail sẽ mở ngay commit reconciliation; queue rework chỉ chạy sau khi Planner đọc candidate/evidence và resolve barrier.',
        'integrator': 'Chỉ nhận candidate đã QA-verified/merge-ready, kiểm tra đúng SHA, base, validation-contract evidence bundle, planning_barrier, retirement và hold. Repository reconciliation đang mở thì không claim/queue/merge. Dùng queued integrator.merge_candidate một lần rồi poll merge_job_id; xác minh trạng thái DONE và mainline SHA. Merge FAILED sẽ tự block và mở Planner job; lỗi durable khác chưa có planner_job thì gọi work.report_failure đúng một lần trước wait. Nếu Planner retire trong lúc merge thì dừng và để HoH reconcile SHA thật trên main. Mỗi merge commit thành công tạo durable delivery_completed report và mở ngay commit reconciliation; execution iteration kế tiếp chỉ mở sau khi Planner đánh giá artifact. Với commit đồng đội đã nằm trên default branch, chỉ record_external_landing sau khi Git xác minh; record này không tạo QA verdict. Nếu drift/conflict hoặc SHA đổi, chuyển về luồng rework/QA; không force-push, merge tay hay sửa DB.'}
    prompts = {}
    for role in ROLES:
        actor = installed_profile_actor(root, checked_project(Path(root), project), project, role, instance)
        prompts[role] = f'''Thiết lập và làm việc với HoH trên máy Windows này.
Vai trò: {role}. Project ID bắt buộc: {project}. Instance: {instance}.
Tôi cho phép cài/cập nhật đúng skill HoH base + {role} và thêm MCP profile này; sao lưu skill HoH cũ, giữ nguyên mọi cấu hình khác.

1. Xác định client là Codex hay Claude Code. Dùng shell PowerShell chạy đúng đường dẫn này, không tự dò hay ghép lại. Dấu `/` trong đường dẫn là chủ ý và PowerShell trên Windows hỗ trợ trực tiếp:
& '{python}' -I -m hoh_control_plane.local_agent setup --root '{root}' --project '{project}' --role {role} --instance {instance} --client codex --upgrade-skills --transport shared-http
Nếu là Claude Code, thay giá trị --client bằng claude-code. Không cài cả hai client nếu không cần.
2. Load $hoh-control-plane và $hoh-{role}. MCP server cần dùng là hoh-{actor.agent_id}. Nếu server chính xác này chưa xuất hiện sau setup thì không thử MCP mặc định, không dùng server role/project khác và không chạy setup lặp lại. Với Codex: giữ HoH đang chạy, Thoát hoàn toàn Codex desktop rồi mở lại; reload task/renderer không nạp lại inventory MCP của app-server. Với Claude Code: reload cửa sổ VS Code/extension host. Sau khi client mở lại, tiếp tục ngay từ bước 3.
3. Gọi session.bootstrap(project_id="{project}") trên đúng MCP đó. Xác minh identity.role="{role}", identity.agent_id="{actor.agent_id}", identity.project_ids chỉ chứa "{project}" và selected_project_id="{project}". Sai bất kỳ mục nào thì dừng trước khi ghi/claim. Nếu role là Planner, ngay sau xác minh phải hoàn tất start barrier trước mọi resume-wait: gọi planner.sync_repository; xử lý từng commit, diff, docs/spec và mọi repository reconciliation đang PENDING; audit toàn bộ ticket được chụp trong observation rồi resolve barrier bằng decision manifest. Chỉ được resume hoặc mở wait khi không còn repository_reconciliation_required và không còn việc hành động.
4. Resume lease/job hiện có trước khi nhận việc khác, ngoại trừ Planner phải hoàn tất start barrier ở bước 3 trước khi resume một idle wait. Đăng ký đúng role/skills gồm `hoh-control-plane` và `hoh-{role}`, capacity=1. Với QA, đọc verification_profile và chỉ khai execution_capabilities mà phiên này đã kiểm tra thật sự có tool/adapter tương ứng; đồng thời truyền machine_id ổn định bằng tên máy từ `[System.Environment]::MachineName`, không dùng ID ngẫu nhiên của chat. Thiếu browser/runtime/domain tool thì không khai khống để nhận việc. Luôn đọc capability_blockers trong inbox và capability_state trong QA discovery trước khi kết luận không có candidate. missing_declaration yêu cầu QA kiểm tra tool thật rồi re-register đúng một lần; phần còn thiếu được Planner provision/route. missing_approval yêu cầu Planner duyệt public preflight evidence trên đúng máy; QA không tự duyệt. Cả hai trường hợp đều đánh thức Planner bằng capability_review_required. Kiểm tra inbox và lọc discovery theo project này. Không tự tạo lại project đã có. Nếu chính chat này đang tiếp tục sau restart/Stop/timeout thì giữ instance {instance}: lấy wait_id trong event_wait của bootstrap và truyền resume_wait_id khi gọi work.wait_for_event, kèm job_kind/job_id nếu đang nối lại job. Chỉ lệnh chờ cũ bị thay; lease, job và worktree giữ nguyên. Nếu wait đã đổi, bootstrap lại một lần; xung đột lặp lại thì báo rõ thay vì tranh giành. Khi nhận status=superseded, dừng yêu cầu cũ và không tự retry. Không kết luận có nhiều chat làm việc chỉ từ số process MCP. Chỉ một chat khác cùng role đang thực sự làm việc song song mới cần instance riêng.
5. Chỉ dùng public specification, repository material và public execution records. Không đưa hidden tests, benchmark score, private rubric/evaluator formula hay evaluator rationale vào ticket, evidence hoặc vòng sau. {role_work[role]}
6. Chỉ dùng tool thật được server công bố, giữ lease khi đang làm việc, lưu checkpoint/handoff bền vững. Không truy cập DB trực tiếp, không sửa worktree của agent khác, không đưa token vào chat. Kết thúc thiết lập bằng cách báo client, MCP server, role, project ID, repository/workspace model và việc sẽ làm tiếp. Không suy diễn MCP có thể tự đánh thức một chat đã dừng.

7. Chế độ GUI nhận sự kiện, không cron: trước khi idle, execution role phải bảo đảm mọi lỗi durable chưa được tool tự mở planner_job đã được gửi đúng một lần bằng work.report_failure; không gửi lỗi điều khiển/transient. Khi hết việc sẵn sàng, thông báo ngắn rồi gọi work.wait_for_event(project_id="{project}", timeout_seconds=43200) và để chat chờ tool. Không kết thúc lượt chỉ vì inbox rỗng, không lặp polling bằng lượt model. Khi có event, resolve context hiện tại và claim theo đúng role; event không cấp lease. Giữ event_id, truyền ack_event_id ở lần wait tiếp theo sau khi đã xử lý hoặc xác định event hết hiệu lực. Khi queue job provision/submission/qa/merge, dùng work.wait_for_event với job_kind và job_id tương ứng, sau đó gọi poll_tool trong kết quả đúng một lần để lấy trạng thái. HoH duy trì lease hợp lệ của job trong lúc tool đang chờ. Nếu cần bạn trả lời thì báo rõ và dừng; không giấu câu hỏi trong một lần chờ 12 giờ. Nếu timeout/mất kết nối, kiểm tra lại host và bootstrap; không tự tạo cron. Đóng chat, bấm Stop, sleep máy hoặc tắt HoH sẽ ngắt chờ. Cách này giữ chính chat Codex app/Claude extension; không tạo phiên CLI thay thế.

Cấu hình này dùng MCP HTTP chung tại loopback, không tạo thêm Python server theo từng chat. Setup tự cấp credential riêng cho identity/project và lưu trong cấu hình client; không chép token vào chat. Mở HoH bằng icon Desktop trước khi kết nối và phải để HoH chạy trong lúc restart client; profile HTTP không tự bật host đã tắt. Máy khác cần bộ cấu hình HTTP riêng và đường mạng đã được cấp quyền.'''
    return prompts


def make_prompts(root, project, instance='primary'):
    """Generate the short operational prompt; detailed policy lives in skills."""
    root_path = Path(root)
    service_root = root_path.as_posix()
    python = (root_path / '.venv' / 'Scripts' / 'python.exe').as_posix()
    focus = {
        'planner': (
            'Run the Planner card after bootstrap and every event: sync repository, '
            'resolve reconciliation before any release/wait, handle Planner jobs and '
            'capability reviews, persist current priorities, then wait. Read the linked '
            'reference only for the event you received.'
        ),
        'developer': (
            'Resume owned lease before new work, implement only in the assigned '
            'worktree, refresh context immediately before one-commit submission, and '
            'stop for a reconciliation barrier or retirement.'
        ),
        'qa': (
            'Declare only verified capabilities on this machine, inspect capability '
            'blockers before reporting no work, test only a frozen claimable candidate, '
            'and record evidence without modifying candidate code or tests.'
        ),
        'integrator': (
            'Merge only the exact verified candidate through the protected job. Verify '
            'the actual mainline SHA after completion and stop for Planner reconciliation.'
        ),
    }
    prompts = {}
    for role in ROLES:
        actor = installed_profile_actor(root, checked_project(Path(root), project), project, role, instance)
        prompts[role] = f'''Set up HoH for this Windows chat.
Role: {role}; project: {project}; instance: {instance}.

1. Identify Codex or Claude Code. Run this exact PowerShell command once; do not
   guess another executable or repeat setup:
   & '{python}' -I -m hoh_control_plane.local_agent setup --root '{service_root}' --project '{project}' --role {role} --instance {instance} --client codex --upgrade-skills --transport shared-http
   For Claude Code, replace only `--client codex` with `--client claude-code`.
2. Load `$hoh-control-plane` and `$hoh-{role}`. Use only MCP
   `hoh-{actor.agent_id}`. If it is absent after setup, keep HoH running and
   fully reopen Codex Desktop, or reload the VS Code extension host; do not use a
   default or different-role server.
3. Call `session.bootstrap(project_id="{project}")`. Confirm role, agent ID,
   project scope and selected project exactly match this prompt. If not, stop
   before registration, claim or write.
4. Register `hoh-control-plane` and `hoh-{role}` once, then inspect inbox/current
   assignment. QA uses the stable host name as `machine_id` and declares only
   equipment it has actually verified. Keep this same instance after restart;
   bootstrap and use `resume_wait_id` instead of creating a duplicate instance.
5. {focus[role]}
6. Use current server packet and exposed tools only. Never use direct SQL, another
   worker's worktree, hidden evaluator material or credentials in chat. Before
   idle wait, report one unhandled durable failure only if no role tool already
   opened a Planner job. In GUI event mode call `work.wait_for_event` only after
   current work is handled; an event is not a lease and a stopped chat cannot be
   revived by MCP.

At setup completion, state the client, exact MCP server, role, project, workspace
model and next action briefly.'''
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
    actor = installed_profile_actor(root, config, args.project, args.role, args.instance)
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
