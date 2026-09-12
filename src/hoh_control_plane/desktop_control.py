"""Opt-in loopback operator controls for the Windows desktop installation."""
from __future__ import annotations

import hmac
import json
from pathlib import Path
import secrets
import threading
import time

from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from hoh_control_plane.api import create_app, ProjectCreate
from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.supervisor import operations_status, set_paused


def create_desktop_app(root: Path):
    root = root.resolve()
    config = json.loads((root / 'config/settings.json').read_text(encoding='utf-8'))
    app = create_app(config['database_path'], local_mode=True, seed_demo=False)
    service = app.state.service
    operator = Actor('desktop-operator', Role.ADMIN, frozenset({'*'}))
    token = secrets.token_urlsafe(32)
    shutdown = root / 'config/shutdown.request'
    control_lock = threading.Lock()
    allowed_hosts = {f"127.0.0.1:{config['ui_port']}", f"localhost:{config['ui_port']}"}

    @app.middleware('http')
    async def local_boundary(request, call_next):
        host = request.headers.get('host', '')
        if host not in allowed_hosts:
            return JSONResponse({'error': 'Invalid local Host'}, status_code=403)
        if request.method not in {'GET', 'HEAD'}:
            if request.url.path not in {'/local/projects', '/local/shutdown'} or request.method != 'POST':
                return JSONResponse({'error': 'Dashboard operation not permitted'}, status_code=405)
            nonce = request.headers.get('x-hoh-local-token', '')
            if (request.headers.get('origin') != 'http://' + host or not nonce.isascii()
                    or not hmac.compare_digest(nonce, token)):
                return JSONResponse({'error': 'Local session proof required'}, status_code=403)
            # Bound streamed bodies too, not only a caller-supplied Content-Length.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16_384:
                    return JSONResponse({'error': 'Request too large'}, status_code=413)
            request._body = bytes(body)
        if request.url.path == '/':
            html = (Path(__file__).parent / 'static/index.html').read_text(encoding='utf-8')
            html = html.replace('</head>', '<link rel="stylesheet" href="/static/desktop.css"></head>')
            html = html.replace('</body>', '<script src="/static/desktop.js" defer></script></body>')
            response = HTMLResponse(html)
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'"
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Referrer-Policy'] = 'no-referrer'
            return response
        response = await call_next(request)
        if request.url.path.startswith('/local/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/__local_installation')
    def installation():
        return {'installation_id': config['installation_id'], 'version': '0.3.1'}

    @app.get('/local/session')
    def session():
        with service.store.connection() as db:
            projects = [dict(row) for row in db.execute('SELECT project_id, name FROM projects ORDER BY name, project_id')]
            running = db.execute("SELECT count(*) FROM worker_executions WHERE status = 'RUNNING'").fetchone()[0]
        return {'csrf_token': token, 'projects': projects, 'shutting_down': shutdown.exists(), 'running_workers': running}

    @app.post('/local/projects', status_code=201)
    def new_project(payload: ProjectCreate):
        with control_lock:
            if shutdown.exists():
                raise HTTPException(409, 'HoH is shutting down')
            return service.create_project(operator, **payload.model_dump())

    @app.get('/local/prompts')
    def prompts(project_id: str):
        from hoh_control_plane.local_agent import make_prompts, checked_project
        checked_project(root, project_id)
        return {'project_id': project_id, 'prompts': make_prompts(root, project_id)}

    @app.post('/local/shutdown', status_code=202)
    def stop():
        with control_lock:
            if not shutdown.exists():
                marker = {'requested_at': time.time(), 'resume_on_start': not operations_status(service)['paused']}
                set_paused(service, True, reason='Desktop shutdown: drain current workers')
                shutdown.write_text(json.dumps(marker), encoding='utf-8')
        return {'status': 'draining'}

    return app
