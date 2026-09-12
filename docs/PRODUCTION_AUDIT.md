# Rà soát production và vận hành HoH — 2026-09-12

## Kết luận

HoH đã có nền tảng điều phối công việc phần mềm tương đối rõ: phân vai, context
durable, lease, candidate SHA, QA/rework, integration gate và bảng quan sát. Phù hợp
nhất hiện tại là pilot có người giám sát, một host thực thi đáng tin cậy, repository
đã đăng ký và test command bắt buộc. Chưa đủ bằng chứng để gọi là hệ thống production
tự vận hành nhiều host, nhiều tenant hoặc tiếp nhận code không tin cậy.

Đánh giá dựa trên code tại base `8538ca84f34cc0aab1fcb08bbc88288ad264c32d`,
test hồi quy và benchmark database tạm. Không sửa database/ticket live hoặc các
worktree của agent đang làm việc. Nhánh thay đổi: `codex/production-readiness`.
Đây là audit theo rủi ro, không phải chứng minh mọi đường chạy đều không còn lỗi.

## Các vấn đề đã sửa

| Ưu tiên | Vấn đề tái hiện/quan sát | Thay đổi |
|---|---|---|
| P1 | HTTP wrapper xác thực nhưng không khởi động lifespan/session manager của SDK | Khởi động/dừng lifespan của các endpoint; test initialize → tools/list → tools/call bằng HTTP thật trong ASGI |
| P1 | Host của reverse proxy bị DNS rebinding protection từ chối | Giữ protection, bổ sung allowlist host/origin cấu hình rõ ràng |
| P1 | Bearer có Unicode làm `compare_digest` ném TypeError | Từ chối credentials không hợp lệ bằng lỗi auth; không trả lỗi server |
| P1 | REST board không yêu cầu auth/phạm vi project ở production | Bắt buộc identity và project authorization; chỉ demo local có observer mặc định |
| P1 | Browser EventSource local trả 401; stream không có kiểm tra disconnect hoạt động | Observer chỉ cho endpoint đọc local; stream async kiểm tra disconnect và có thể cancel khi đóng trang |
| P1 | PostgreSQL `BEGIN` bị hiểu như khóa ghi `BEGIN IMMEDIATE` | Khóa advisory theo transaction/schema để giữ đúng mô hình single writer hiện tại; chưa phải tăng throughput ghi |
| P1 | Tạo Git worktree giữ khóa ghi DB, làm chậm heartbeat/claim khác | Đưa Git ra ngoài transaction, kiểm tra lại lease/version/assignment trước khi công bố |
| P1 | Agent A quay lại sau A → B → A bị trùng path/branch | Worktree có generation riêng cho từng lần tạo; giữ nguyên thư mục cũ và dữ liệu chưa commit |
| P1 | Job QUEUED/RUNNING của lease chết giữ unique slot mãi | Fence job provisioning/submission/QA khi thu hồi lease; queued merge cũng được đóng; running merge cần đối soát |
| P1 | Candidate ở agent_workspace gửi được nhưng QA thiếu worktree server | QA tạo detached checkout của đúng SHA theo test-run ID và dọn tài nguyên của run sau khi kết thúc |
| P1 | Candidate test kế thừa token map, DB URL và identity qua biến HOH_* | Bỏ các biến control-plane khỏi môi trường test; đây không phải sandbox |
| P1 | Postgres worker nhận DSN trong argv, làm lộ password trong process listing | Truyền DSN qua môi trường worker; argv chỉ giữ database hint không có secret |
| P2 | Chat mới phải mò project/workspace/job bằng lịch sử chat | Thêm `session.bootstrap`, đọc có giới hạn và đúng project/identity; không tự claim |
| P2 | Skill Developer luôn yêu cầu service worktree, kể cả workspace máy riêng | Hướng dẫn theo workspace model và cách resume/takeover |
| P2 | Bộ cài cũ gắn với path máy phát triển; wheel không chứa skills | Wheel kèm skills; builder xuất kit ZIP có manifest/checksum, cấu hình MCP và installer Python |
| P2 | Inbox limit chỉ giới hạn response, vẫn tạo offer cho toàn backlog | Giới hạn cả việc tạo offer, truy vấn trực tiếp agent đang poll |
| P2 | Snapshot cũ về muộn có thể ghi đè dashboard/ticket mới | Kiểm tra generation của request và project trước khi cập nhật |
| P2 | Chưa có bộ QA trình duyệt chạy lặp lại được | Suite browser với fixture DB/server riêng, SHA/dirty flag, ảnh/trace/checksum; ba viewport và kiểm tra hành vi |
| P2 | Đường dẫn evidence ngoài worktree có thể làm pytest chọn config repo cha | Lệnh QA UI chỉ định `-c pyproject.toml` của candidate |

## Kết quả đo giới hạn

Chạy cùng script `scripts/benchmark-control-plane.py`, SQLite local, 1.000 ticket
synthetic, 10 lần đọc, một client, cùng máy. Không phải load test production hay SLO.

| Phép đo | Trước | Sau |
|---|---:|---:|
| Inbox 20 — lần đầu | 140,25 ms | 11,87 ms |
| Inbox 20 — trung vị | 51,80 ms | 8,82 ms |
| Tổng offer được lưu sau 10 poll | 1.000 | 20 |
| Board — trung vị | 21,78 ms | 22,04 ms |
| Board — payload | 797.463 byte | 797.463 byte |
| Discover 20 — trung vị | 5,10 ms | 4,29 ms |

Mức cải thiện có ý nghĩa ở inbox; chênh lệch nhỏ của board/discovery có thể là nhiễu.
Board vẫn tăng theo toàn bộ số ticket. Không suy ra được số agent tối đa hay TPS
production từ các phép đo này. Các file JSON kết quả nằm trong release `dist/`.

## Worktree: ranh giới thực tế

1. **Cùng host:** service và Developer nhìn cùng filesystem. Path/branch assignment
   do HoH trả về là nguồn xác định nơi sửa code. Root checkout dùng integration phải
   sạch và ở default branch; các agent không làm trực tiếp vào đó.
2. **Khác host:** `agent_workspace` cần Git remote và quyền Git bên cạnh MCP auth.
   Push lên GitHub/GitLab không tự đưa SHA vào clone mà service đang đọc; cần fetch
   adapter hoặc đường nhận Git do operator quản lý. Git authorization không nằm
   trong bearer token của HoH.
3. **Resume:** lấy lease/job IDs hiện tại, refresh context, heartbeat/poll cùng ID.
   Không tạo worktree mới nếu assignment hiện tại đủ dùng. Dirty work phải được
   xem xét và giữ lại; không reset để làm sạch test.
4. **Takeover:** cấp generation mới. Lease hết hạn không chặn được process cũ ghi
   vào filesystem; cần supervisor/OS isolation nếu muốn fencing vật lý. DB chỉ
   ngăn công bố candidate từ lease đã mất.
5. **Provision bị gián đoạn:** Git nằm ngoài DB nên có thể còn checkout chưa được
   công bố nếu lease/version thay đổi hoặc process chết. Giữ nó để phục hồi;
   chưa có bộ thu gom tự động có retention và xác minh an toàn. Không được xóa
   recursively cả worktree root hay prune worktree của agent khác.
6. **QA:** workspace từ máy riêng được kiểm tra trong detached checkout trên host
   runner. Chế độ colocated vẫn kiểm tra worktree Developer đang giữ. QA không có
   sandbox OS; quy trình phải ngăn Developer tiếp tục sửa trong thời gian QA.

## Rủi ro còn lại trước production rộng

| Mức | Rủi ro / vị trí | Việc cần làm và điều kiện nghiệm thu |
|---|---|---|
| P0 với code không tin cậy | `qa_runner.py`: test command chạy cùng user OS, vẫn đọc được filesystem/home và có thể sinh process con | Worker container/VM/user riêng, secrets tối thiểu, timeout cả process tree, CPU/RAM/disk/network quota; thử fault injection và kiểm tra không chạm DB/credentials |
| P1 | PostgreSQL chưa được xác nhận bằng server thật trong audit này | Chạy conformance + test concurrent overlap trên DB tạm với `HOH_TEST_POSTGRES_DSN`; test migrations, backup/restore và failure recovery |
| P1 | `integration.py` + `merge_jobs`: DB/Git không có transaction chung | Đối soát job RUNNING sau crash bằng candidate/base/mainline SHA; kiểm chứng crash trước/sau FF và không merge lặp |
| P1 | Store nhiều host nhưng artifact/worktree vẫn local; worker chưa có host affinity | Registry runner/host, artifact object storage, delivery/fetch Git và routing job; thử failover giữa hai host |
| P1 | `mcp_server.py`: Popen theo job, chưa có worker pool/supervision/backpressure toàn cục | Worker có giới hạn concurrency, heartbeat, hàng chờ bền, dead-letter; chứng minh recover sau kill -9/reboot |
| P1 | Auth là bearer token tĩnh, identity không phân biệt hai chat cùng credential | Lifecycle/rotation/revocation, định danh worker/session và cảnh báo duplicate identity; OIDC/OAuth nếu cần người dùng tự đăng nhập |
| P1 | Project không đăng ký repository/test policy vẫn hỗ trợ luồng MVP với bằng chứng khai báo | Operator phải khóa onboarding production vào repository thật, test command và release policy có proof; test không thể đi DONE chỉ bằng khai báo |
| P2 | `board.py`, `app.js`: full snapshot + render toàn bộ + poll 10 giây | Pagination/filter server, summary riêng, cache/ETag; thử 10k ticket và nhiều observer, đo p95/p99 và event lag |
| P2 | `discovery.py`: scheduler dispatch còn quét backlog/agent và nhiều query | Batch/keyset, index theo workload và claim transaction theo phạm vi; đo fairness/starvation cùng throughput |
| P2 | Audit/log/artifact tích lũy; chưa có retention/GC/quota | Chính sách retention, archive và kiểm chứng recovery trước GC; không xóa evidence đang được tham chiếu |
| P2 | README/deployment trước audit đã chậm hơn implementation | Giữ runbook + artifact kit version cùng release; smoke test kit từ wheel trong môi trường mới |

PostgreSQL advisory lock hiện tại chủ động giữ **một writer cho mỗi schema**. Nó
sửa tính đúng đắn nhưng không tạo write scaling. Muốn bỏ khóa này phải chuyển các
read/check/write invariant sang row/predicate locking hoặc atomic compare/update,
đặc biệt scope conflict trên **hai ticket khác nhau**, không chỉ unique lease cùng ticket.

## Con người có quản lý và nắm tiến độ được không?

Khá tốt ở cấp ticket: biết ai đang giữ lease, chờ vai nào, candidate nào, lỗi QA
nào và context tiếp tục. Dashboard thêm tổng số ticket DONE/total, đang xử lý,
chờ QA, chờ integration và blocked. Những số này phản ánh trạng thái ticket,
không phải phần trăm hoàn thành yêu cầu kinh doanh hoặc dự báo ngày xong.

Để quản lý dự án thật cần đo riêng: lead/cycle time, tuổi hàng chờ, thời gian blocked,
throughput ticket được tích hợp, tỷ lệ first-pass QA/rework, defect thoát ra production,
chi phí model/worker và yêu cầu chưa được cover. Không dùng số tool call hoặc số
agent online làm năng suất. Coverage requirement và audit đã có nền; chưa có view
điều hành tổng hợp các chỉ số trên hay burn-up đáng tin cậy.

Người quản lý nên kiểm soát mục tiêu/ưu tiên, duyệt contract, giải quyết blocker và
đánh giá evidence. Planner chia ticket quá nhỏ hoặc có scope chồng lấn sẽ làm
chi phí điều phối tăng và throughput giảm. Chưa có dữ liệu project chạy thật để
khẳng định tiết kiệm bao nhiêu phần trăm thời gian hoặc chi phí.

## So với automation công nghiệp

Về cấu trúc, HoH giống một lớp điều hành sản xuất phần mềm: work order = ticket,
dispatch = cấp việc, worker = tài nguyên thực thi, QA = kiểm tra chất lượng,
rework = sửa hàng lỗi, candidate SHA = truy xuất lô sản phẩm, dashboard = giám sát.
Đó là phép so sánh với hoạt động MES/MOM ở lớp vận hành trong ISA-95.

Chưa tương đương PLC/SCADA hoặc dây chuyền công nghiệp vận hành ổn định: agent có
đầu ra không tất định, lease không dừng máy/process, chưa có điều khiển thời gian
thực, interlock vật lý, supervisor HA, định mức tài nguyên hay bằng chứng fault
recovery đầy đủ. Đây là **workflow orchestration có quality gate**, đang tiến về
automation có phản hồi; không phải chứng nhận tuân thủ ISA-95 hay hệ thống safety.

Nguồn đối chiếu: [ISA-95](https://www.isa.org/standards-and-publications/isa-standards/isa-95-standard),
[PostgreSQL locking](https://www.postgresql.org/docs/current/explicit-locking.html),
[Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
[Playwright test runners](https://playwright.dev/python/docs/test-runners).

## Phân loại tài sản theo module

| Module | Nhận định | Hướng xử lý |
|---|---|---|
| domain, scopes, service context/planning/leases | Core Asset | Giữ domain invariant và contract tests |
| SQLite/store seam | Core Asset | Phù hợp pilot một host, cần migration/restore gates |
| PostgreSQL adapter | Rebuild phần concurrency trước mở rộng | Giữ interface; khóa tạm đúng contract, chưa tối ưu writer |
| MCP/API/auth | Core Asset | HTTP lifecycle đã sửa; thêm lifecycle credentials/rate limits |
| Git worktrees và integration | Core Asset | Giữ immutable SHA; bổ sung fencing, orphan inventory và crash reconciliation |
| QA workers/runner | Extract & Merge vào worker runtime riêng | Tách đặc quyền khỏi control plane, quản lý browser/process quota |
| static dashboard | Core Asset | Có UI acceptance; cần server pagination và project metrics |
| skills/agent kit | Core Asset | Phân phối cùng wheel, kiểm tra checksum/install/resume |
| deploy hardcoded profiles | Deprecate làm gói phân phối chung | Chỉ là cấu hình local mẫu; dùng kit portable cho phiên mới |
| tests | Core Asset | Giữ contract/regression; bổ sung browser và PG concurrent thực |
| data/worktrees, .codex/.claude, venv/cache | Runtime/build artifacts | Loại khỏi source audit/release; giữ nguyên dữ liệu live |
| PDF nghiên cứu đi kèm | Tài liệu tham khảo bên ngoài | Không phải thư viện runtime/vendored code |

Không thấy thư viện third-party nhúng vào source runtime; dependencies khai báo
qua Python packaging. Audit này không thay thế dependency vulnerability scan/SBOM.

## Kiểm chứng bản sửa

Xem [VALIDATION.md](VALIDATION.md) cho kết quả cuối cùng, các phần bị skip và giới
hạn môi trường. Xem [AGENT_KIT.md](AGENT_KIT.md) để cài cho phiên chat mới, và
[UI_QA.md](UI_QA.md) để chạy browser acceptance trên candidate.
