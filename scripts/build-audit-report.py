"""Render a searchable source inventory alongside the human production audit."""
import html
import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
names = subprocess.check_output(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard"], text=True).splitlines()
groups = {}
for name in sorted(set(names)):
    path = root / name
    if not path.is_file() or name.endswith("PRODUCTION_AUDIT.html"):
        continue
    if name.startswith("src/hoh_control_plane/service/"):
        group = "Domain services"
    elif name.startswith("src/hoh_control_plane/static/"):
        group = "Operator dashboard"
    elif name.startswith("src/"):
        group = "Transport, stores and execution"
    else:
        group = {"tests": "Tests", "skills": "Portable skills", "scripts": "Tooling",
                 "deploy": "Deployment profiles", "docs": "Documentation"}.get(name.split("/")[0], "Repository metadata and references")
    rows = groups.setdefault(group, [])
    binary = path.suffix.lower() == ".pdf"
    rows.append({"path": name, "bytes": path.stat().st_size,
                 "lines": None if binary else len(path.read_text(encoding="utf-8", errors="replace").splitlines())})
cards = []
for group, files in groups.items():
    verdict = "Deprecate distribution templates" if group == "Deployment profiles" else "Core asset / inspect risks"
    rows = "".join(f'<tr data-path="{html.escape(row["path"], quote=True)}"><td>{html.escape(row["path"])}</td><td>{row["lines"] or "—"}</td><td>{row["bytes"]:,}</td></tr>' for row in files)
    cards.append(f'<details open class="module"><summary>{html.escape(group)} <small>{len(files)} files · {verdict}</small></summary><table><thead><tr><th>File</th><th>Lines</th><th>Bytes</th></tr></thead><tbody>{rows}</tbody></table></details>')
count = sum(len(files) for files in groups.values())
page = '''<!doctype html><html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>HoH · Production audit</title><style>
:root{font-family:Segoe UI,system-ui,sans-serif;color:#1c2e40;background:#f2f5f8}*{box-sizing:border-box}body{margin:0}main{max-width:1180px;margin:auto;padding:36px 24px}h1{font-size:36px;margin:8px 0 18px}p{line-height:1.6;max-width:920px}.eyebrow{color:#4c6380;letter-spacing:.1em;font-size:12px;font-weight:700}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin:24px 0}.card,details{background:white;border:1px solid #dbe2ea;border-radius:12px;padding:18px}.card strong{display:block;font-size:27px;margin-bottom:7px}.card span,small{color:#566a80}a{color:#245cb5}input{width:100%;padding:14px;border:1px solid #bac8d8;border-radius:8px;font:inherit;margin:18px 0}details{margin:12px 0}summary{cursor:pointer;font-weight:650}small{display:block;font-size:12px;font-weight:400;margin-top:6px}table{border-collapse:collapse;width:100%;margin-top:14px;font-size:13px}td,th{padding:9px;text-align:left;border-bottom:1px solid #e6edf3}td:first-child{overflow-wrap:anywhere}td:not(:first-child),th:not(:first-child){text-align:right;white-space:nowrap}.note{border-left:4px solid #cb9636;padding:12px 18px;background:#fff8e8}footer{color:#566a80;font-size:12px;margin-top:28px}
</style><main><div class="eyebrow">HOH CONTROL PLANE · 12 SEPTEMBER 2026</div>
<h1>Production readiness & source inventory</h1>
<p>Phù hợp pilot một host có người giám sát. Chưa đủ bằng chứng cho production nhiều host hoặc code không tin cậy. Các thay đổi tập trung vào auth, khởi động phiên, worktree, QA và giới hạn inbox.</p>
<div class="cards"><div class="card"><strong>52 → 9 ms</strong><span>Trung vị inbox, 1.000 ticket synthetic</span></div><div class="card"><strong>1.000 → 20</strong><span>Offer được tạo với inbox limit 20</span></div><div class="card"><strong>3 viewports</strong><span>QA UI trên Chrome thật: 1440 / 768 / 375</span></div><div class="card"><strong>COUNT files</strong><span>Source/document inventory, loại runtime data</span></div></div>
<p class="note">Giới hạn còn lại: worker sandbox và supervision; đối soát Git/DB sau crash; PostgreSQL cần conformance trên server thật; artifact/host routing; board còn tải toàn bộ ticket. Số DONE/total là tiến độ ticket, không phải dự báo hoàn thành dự án.</p>
<p><a href="PRODUCTION_AUDIT.md">Báo cáo và mức ưu tiên</a> · <a href="VALIDATION.md">Kết quả kiểm chứng</a> · <a href="AGENT_KIT.md">Cài phiên chat mới</a> · <a href="UI_QA.md">Chạy QA UI</a></p>
<label for="filter">Lọc module/file</label><input id="filter" placeholder="Ví dụ: worktree, auth, tests/ui" autocomplete="off">
CARDS
<footer>Inventory từ Git và các file mới của nhánh audit. Không quét credentials, database live hoặc worktree của các agent khác. Số liệu timing đơn client không phải SLO production.</footer></main>
<script>document.querySelector('#filter').addEventListener('input',event=>{const term=event.target.value.toLowerCase();document.querySelectorAll('.module').forEach(module=>{let shown=0;module.querySelectorAll('tbody tr').forEach(row=>{row.hidden=!row.dataset.path.toLowerCase().includes(term);if(!row.hidden)shown++});module.hidden=shown===0;module.open=true})});</script></html>'''
(root / "docs/PRODUCTION_AUDIT.html").write_text(page.replace("COUNT", str(count)).replace("CARDS", "\n".join(cards)), encoding="utf-8")
print(json.dumps({"files": count, "modules": {group: len(files) for group, files in groups.items()}}))
