# Kiểm chứng HoH 0.2.0 — 2026-09-12

Nhánh `codex/production-readiness`, base
`8538ca84f34cc0aab1fcb08bbc88288ad264c32d`. Toàn bộ kiểm chứng dùng checkout
riêng, repository/database fixture hoặc thư mục client tạm; không dùng database
production. SHA của bản phát hành và evidence nằm trong `release-manifest.json`
của ZIP phát hành, tránh tự tham chiếu SHA trong chính commit.

## Backend và contract

```sh
python -m pytest -c pyproject.toml -q --cov=hoh_control_plane --cov-report=term --cov-fail-under=80
```

Kết quả cuối: **660 passed, 25 skipped, 3 warnings; 448,39 giây**.
Coverage do pytest-cov báo: **86,14%**, vượt gate 80%.

- 20 test PostgreSQL bị skip vì không có server/`HOH_TEST_POSTGRES_DSN`.
  Có thêm test hai ticket khác nhau có file scope chồng lấn được claim đồng thời;
  test này chưa chạy trên PostgreSQL thật. Không suy diễn SQLite pass thành PG pass.
- 5 test UI mặc định skip; chúng chạy riêng với `--run-ui` như bên dưới.
- Warning gồm hai deprecation của test dependencies và một ResourceWarning SQLite
  connection khi chạy suite. Coverage còn báo package đã được import trước lúc
  đo. Cần theo dõi riêng các warning này; không xem coverage là bằng chứng không
  có leak hoặc bao phủ mọi nhánh, và không coi đây là một lượt chạy không warning.

Các regression quan trọng: HTTP initialize/list/call và lifespan thật; auth Unicode
và board project scope; worker argv không chứa PostgreSQL credential; session resume
cho lease/planner job; worktree A → B → A giữ dirty work; Git provisioning không
giữ DB write lock; job slot được giải phóng khi lease hết hạn; remote candidate QA;
inbox tạo offer có giới hạn; bộ cài portable cho 4 role × 2 client.

## Browser

```sh
python -m pytest -c pyproject.toml tests/ui --run-ui --ui-channel chrome --no-cov --ui-artifacts <absolute-directory-outside-checkout>
```

Development run: **5 passed trong 12,98 giây**, trên Chrome thật ở các viewport
1440 × 900, 768 × 900 và 375 × 812. Suite kiểm tra tương tác điều hướng/filter,
context ticket, reload, bàn phím cơ bản, overflow, GET-only, stale response,
empty/error state. Đây không phải visual-baseline comparison hoặc accessibility
audit toàn diện.

Gói phát hành chứa một lượt UI chạy lại trên commit sạch: mỗi scenario có SHA,
`source_tree_dirty`, fixture identity, browser version, screenshot, Playwright trace,
request/error log và checksum. Xem `evidence/ui/`, `evidence/ui-tests.log` và
`release-manifest.json` trong ZIP để xác nhận đúng artifact. Không dùng URL live
làm bằng chứng của candidate. Suite fixture chỉ hỗ trợ UI HoH; project khác phải
đăng ký browser suite của chính project đó.

## Packaging và kiểm tra bổ sung

- Build wheel và sdist bằng Hatchling; wheel chứa skills/references/installer.
- Cài wheel vào thư mục Python tạm và sinh kit từ assets trong wheel.
- Kit cho Planner, Developer, QA, Integrator có checksum; cài vào client home tạm,
  giữ unrelated settings, chạy lại idempotent, từ chối conflict/tampering.
- Sdist dùng allowlist; không chứa root `.mcp.json`, live database, worktree,
  credentials, venv hoặc tài liệu PDF ngoài runtime.
- `node --check src/hoh_control_plane/static/app.js` và `git diff --check` qua.
- Skill validator: base, Developer, QA hợp lệ.
- Không cài đè cấu hình client thật, không thay service đang chạy và không kiểm
  chứng việc bật kit trong một phiên Codex/Claude Code mới qua UI của client.

Kit kèm release trỏ tới `http://127.0.0.1:8931/mcp` cho pilot cùng host. Operator
cần chạy đúng server release, cấu hình repository/role/token; khi dùng host khác,
build lại kit với HTTPS URL thật theo [AGENT_KIT.md](AGENT_KIT.md).

## Môi trường và phép đo

Windows; Python 3.13.0; FastAPI 0.141.1; MCP SDK 1.30.0; Pydantic 2.13.5;
Uvicorn 0.52.4; pytest 8.4.2; pytest-cov 6.3.0; pytest-playwright 0.9.0;
Playwright 1.62.0; Hatchling 1.32.0. Dùng browser Chrome đã cài trên máy.

Benchmark SQLite tạm, 1.000 ticket, một client, 10 lượt đọc: inbox trung vị
51,80 → 8,82 ms, offer được lưu 1.000 → 20. Board vẫn khoảng 797 KB và 22 ms.
JSON trước/sau được giữ trong ZIP. Đây là microbenchmark, không phải xác nhận
throughput/SLO/ROI production. Xem [PRODUCTION_AUDIT.md](PRODUCTION_AUDIT.md)
cho rủi ro còn lại và điều kiện nghiệm thu triển khai rộng.
