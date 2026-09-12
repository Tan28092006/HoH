# Kiểm chứng vận hành HoH 0.3.0 — 2026-09-12

Nhánh `codex/operational-recovery`, base
`9ad9f65ed053756bbfd2ec499b003ce222cd22af`. Đây là bước bổ sung cho audit 0.2.0.
Không triển khai vào service live hoặc thay database/worktree đang làm việc.
SHA chính xác của release nằm trong `release-manifest.json` trong ZIP.

## Phạm vi được kiểm chứng

- QA dùng snapshot Git riêng của candidate SHA, không dùng thư mục Developer
  đang sửa; từ chối ghi đè snapshot có sẵn. Lease mới tạo assignment mới và giữ
  dirty work cũ, kể cả dùng lại cùng identity.
- Merge ghi intent bền vững trước Git và giữ khóa OS dùng chung qua git-common-dir.
  Crash sau khi cập nhật mainline được đối soát một lần; crash trước Git không tự
  merge. Lease hết hạn, ticket thay đổi và repository dirty đều có test riêng.
- Windows Job Objects: timeout giết cây tiến trình, hoàn tất cũng dọn tiến trình
  nền, mất quyền job/lease hủy worker; kill owner thật chấm dứt cây con. Output
  giữ trong bộ nhớ có giới hạn. Đây là kiểm thử native Windows, không cần VM.
- Supervisor: chạy queue thật, giới hạn concurrency, khóa một owner, pause/resume,
  launch failure không lặp vô hạn, kill/restart tiến trình thật, phục hồi durable
  job, heartbeat và health probe. Host khác hoặc sai tên worker mode bị từ chối.
- MCP supervised mode chỉ enqueue; cần cấu hình chế độ này trên mọi MCP process.
  Legacy mode vẫn là mặc định tương thích và không có bảo đảm supervisor.

## Backend và PostgreSQL

Lượt toàn bộ suite có PostgreSQL 17.10 thật: **704 passed, 5 skipped, 3 warnings**,
457,85 giây. Năm test UI chạy riêng. Tuy nhiên lượt này **exit 1** vì bước ghép
coverage phát hiện shard statement/branch khác cấu hình; không ghi nhận là gate
coverage thành công. Đã sửa cấu hình bằng `--cov-branch` và đường dẫn
`--cov-config` tuyệt đối cho cả worker subprocess.

Lượt đo lại trên máy thiếu tài nguyên dừng sau 232 test pass với WinError 1450;
không suy diễn lượt dở dang thành pass toàn bộ. Log của cả hai lần được giữ trong
release. Docker PostgreSQL thử nghiệm đã dừng theo yêu cầu của người dùng.
Không cài PostgreSQL native hoặc bật lại WSL để đo tiếp.

Gate native tại commit `54d1c8ee7abff46df66f89a45aa015c2ec496367`: **684 passed,
25 skipped, 3 warnings; coverage 84,89%; 495,48 giây; exit 0**. Các commit sau
chỉ sửa focus dashboard, thêm browser regression và cập nhật tài liệu; backend
Python giữ nguyên. Kết quả native và QA UI được ghi trong release manifest và log
`evidence/native-tests.log`, `evidence/ui-tests.log`. Command tái lập:

```powershell
python -m pytest -c pyproject.toml -q --cov=hoh_control_plane --cov-branch --cov-config=<absolute-pyproject.toml> --cov-report=term --cov-fail-under=80
python -m pytest -c pyproject.toml tests/ui --run-ui --ui-channel chrome --no-cov --ui-artifacts <absolute-directory-outside-checkout>
```

Đặt `COVERAGE_FILE` vào thư mục riêng cho từng lượt đo, tránh ghép shard còn lại
của lượt chạy lỗi. Bỏ `HOH_*` môi trường live trước khi chạy; chỉ cấp
`HOH_TEST_POSTGRES_DSN` của DB test khi thực sự kiểm tra PostgreSQL. Các test PG
tạo schema riêng rồi dọn. Các kiểm thử supervisor mới hiện chạy trên SQLite;
20 test PostgreSQL là conformance/concurrency hiện có, chưa bao phủ mọi crash
scenario supervisor trên PG.

## UI, kit và artifact identity

QA UI dùng Chrome đã cài trên máy, fixture DB và server loopback riêng; xác nhận
import đúng checkout. Lượt đầu trên commit 54d1c8e có 1 fail/4 pass: background
refresh thay DOM của ticket làm mất focus bàn phím. Browser regression tái hiện
lỗi trước sửa; dashboard hiện giữ focus trên cùng ticket, hoặc chuyển về bộ lọc
status khi ticket không còn trong view. Không bỏ qua assertion bàn phím.

Sáu case cuối gồm các viewport 1440/768/375, filter/context, keyboard qua refresh,
empty/error và stale response. Mỗi case giữ screenshot, trace,
SHA, trạng thái Git sạch, request/errors và checksum. Đây chưa phải kiểm thử
visual baseline hoặc accessibility đầy đủ. Các project khác cần browser suite
của chính project đó; bộ fixture này chỉ kiểm chứng dashboard HoH.

Wheel/sdist và bốn kit được tạo từ candidate sạch. Kit dùng assets từ wheel đã
cài vào thư mục tạm; cài hai lần vào home tạm cho Codex và Claude Code để kiểm
tra idempotency. Không chứa credentials hoặc runtime DB, không ghi cấu hình
client thật. Chat mới vẫn cần token, URL MCP đúng và Git/filesystem/tool access
cho assignment; auth MCP không tự cấp môi trường thực thi.

## Giới hạn kết luận

Đủ cơ sở cho pilot một host có người giám sát; **chưa đạt bảo đảm tương đương
dây chuyền công nghiệp có phục hồi đầy đủ**. Chưa đo SLO, RPO/RTO, HA, tải kéo dài,
đầy đĩa, reboot host, outage DB/network. Nhánh POSIX của process manager chưa chạy
trên Linux thật. Supervisor không quản lý các chat Developer chạy bên ngoài,
không có sandbox hay quota tài nguyên từng worker. Chưa tự cài OS watchdog,
gửi cảnh báo, GC artifact hoặc tổ chức incident ownership.

Reconciliation chỉ bảo đảm cho queued merge worker. Legacy synchronous merge
chưa có durable intent. Merge còn mơ hồ giữ ticket của nó để operator xử lý;
chưa tự quarantine mọi ticket khác trong repository. Error domain nằm trong
job DB; stdout/stderr worker bất ngờ chưa được giữ thành log tập trung.

Warnings về dependency deprecation, SQLite connection và coverage import được
giữ nguyên trong log; pass test không chứng minh không còn leak hoặc mọi nhánh
đều đúng. Xem [runbook](RUNTIME_OPERATIONS.md) để cấu hình và vận hành.
