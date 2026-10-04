# Rà soát kiến trúc harness HoH — 2026-10-04

Snapshot tại `main` `2fa75f3` (0.3.25, đã cài trên host ngày 2026-09-26). Đây là
tài liệu snapshot: số dòng dẫn chứng đúng tại commit này và sẽ lệch khi code đổi.
Bản đồ hiện hành nằm ở [ARCHITECTURE.md](../ARCHITECTURE.md).

## 1. Phạm vi và phương pháp

Hai lăng kính:

- **System design** (*System Design Interview Vol. 2*, Alex Xu & Sahn Lam): chọn
  các câu hỏi checklist của chương 04 (message queue: ordering, delivery,
  replay), 06 (aggregation: crash giữa output và checkpoint), 07 (reservation:
  tranh chấp tài nguyên cuối cùng), 11 (payment: idempotency, ambiguity với hệ
  thống ngoài, reconciliation) và 12 (wallet: reproducibility, giao dịch dở dang).
  HoH không phải hệ thống tiền tệ, nhưng có cùng cấu trúc bất biến: lease giống
  phòng cuối cùng, Git là "PSP" bên ngoài mà control plane phải đối chiếu, và
  merge là một side effect không đảo ngược.
- **Agent harness** (*AI Agents in Depth*, Bojie Li; WalkingLab *Learn Harness
  Engineering*): năm hệ thống con Instructions / Tools / Environment / State /
  Feedback (L02), Fresh Session Test (L03), instruction routing (L04),
  completion gates (L09), ACI/poka-yoke và async lifecycle (chương 4),
  multi-agent handoff (chương 10), context engineering (chương 2).

Evidence: đọc source (ba audit song song cho state/concurrency, workflow/async,
giao diện agent/context; các phát hiện nghiêm trọng đã được kiểm lại trực tiếp
trong code), test suite, ghi chép vận hành trong memory của dự án. MCP của HoH
không kết nối được trong phiên này, nên **không** kiểm trạng thái live board.

Mức kết luận theo checklist của sách: **đáp ứng ở mức thiết kế**, **đã kiểm
chứng** (có test cụ thể), **chưa đáp ứng**, **chưa đủ bằng chứng**,
**không áp dụng**.

## 2. Tóm tắt

**Điểm mạnh thật sự** (đã kiểm chứng bằng test):

- Role gating được enforce ở server: mỗi bearer credential có một FastMCP instance
  riêng, tool ngoài role không tồn tại; service còn `require_role` (~59 chỗ).
- Lease là fencing token: mọi write hệ quả, kể cả completion bất đồng bộ, kiểm
  `lease_id` + `expected_version`. Có test takeover thật.
- Merge queued đi theo mẫu đúng của chương 11: ghi `merge_intents` cùng
  transaction với RUNNING, chạy git **ngoài** transaction, đối chiếu với SHA thật
  của Git khi recover; git không chạy hai lần (`test_integration_async.py`).
- Idempotency key gắn với hash payload; dùng lại key với payload khác bị từ chối.
- Event wait là level-triggered, socket bind trước khi kiểm, delivery
  at-least-once có ack.
- Packet theo role có ngân sách, snapshot content-addressed, freshness check.

**Rủi ro ưu tiên** (chi tiết ở §3–§4, lộ trình ở §6):

1. Wake-up có thể mất vĩnh viễn sau barrier (khóa dedup theo `queue_id`).
2. Merge đã vào Git nhưng job kẹt RUNNING nếu barrier mở trong lúc gate chạy.
3. Git subprocess chạy bên trong write lock toàn cục (đường `submit_candidate`
   đồng bộ, REST) — một lệnh git chậm làm nghẽn mọi heartbeat và claim.
4. Ba writer (`create_project/feature/ticket`) không đi qua write lock, nên có
   thể lọt khỏi reconciliation boundary; trên PostgreSQL còn làm hỏng thứ tự
   `sequence` mà cursor dựa vào.
5. Giao diện agent nói sai với server: skill và docstring bảo `PASS`/`FAIL`,
   server chỉ nhận `passed`/`failed`; reference bị cắt section không resolve
   được; MCP bỏ `details` của lỗi.

**Nút thắt harness lớn nhất không nằm ở code mà ở chi phí vòng lặp.** Chủ dự án
dừng pipeline ngày 2026-09-26 vì mỗi ticket mất khoảng 30 phút: test chạy ba lần,
QA lái browser cho từng scenario, merge fast-forward tuần tự. Đây là quan sát vận
hành, không phải phép đo có kiểm soát, nhưng nó là outcome metric duy nhất hiện
có và HoH chưa tự đo nó.

## 3. Đối chiếu checklist system design

Mỗi dòng: câu hỏi → câu trả lời của hệ thống → tiêu chí nguồn (trang PDF) →
evidence → kết luận → khoảng thiếu.

### 3.1 Ordering, delivery và replay (chương 04, chương 06)

| ID | Câu trả lời của HoH | Tiêu chí nguồn | Evidence | Kết luận | Khoảng thiếu |
| --- | --- | --- | --- | --- | --- |
| 04-TĐ3 Ordering/delivery semantics? | Event có `sequence` tăng dần; wake-up at-least-once, pending tới khi ack | PDF 98–99: chốt phạm vi ordering và at-least-once, không hứa global order mơ hồ | `audit_events.sequence` (store.py:904-918); `agent_event_deliveries` (event_wait.py:398-403); `test_event_wait.py:44,190,203` | Đã kiểm chứng cho SQLite | Trên PostgreSQL, writer không lấy advisory lock có thể commit `sequence` thấp sau `sequence` cao; cursor `sequence > after` (board.py:125) bỏ qua vĩnh viễn. Chưa có test đa luồng |
| 04-DD1 Consumer chết sau output / dedup làm mất tin? | Dedup theo `event_key = 'queue:'+queue_id`, kiểm `exists` ở mọi trạng thái | PDF 105–129: offset/ack và dedup phải không làm mất tin hợp lệ | event_wait.py:364, 409; queue row được tái dùng qua offer expiry và barrier cancel (discovery.py:1095-1104, planning.py:421-436) | **Chưa đáp ứng** | Developer đã ack offer Q, bị barrier chặn, sẽ không được đánh thức lại cho Q sau khi Planner resolve. Sửa: khóa theo `offer_id` hoặc chỉ dedup trạng thái pending; test barrier → resolve → wake |
| 04-TĐ2 Replay/retention? | `audit_events` append-only theo quy ước; payload mỏng | PDF 98–99: replay cần durable log và vị trí consumer độc lập | Không có code projection/replay; payload ví dụ `{"lease_id"}` (leases.py:330) | Không áp dụng như event sourcing; **chưa đủ bằng chứng** cho audit đầy đủ | Không tái dựng được lịch sử ticket từ events. Chấp nhận được nếu audit chỉ để quan sát, nhưng events đang được dùng làm bằng chứng phân quyền qua `LIKE '%ref%'` (planning.py:2854, integration.py:769-775) |
| 06-DD1 Crash giữa output và checkpoint? | Idempotency: reserve → `run()` → complete là ba transaction riêng | PDF 176–194: output và checkpoint cần ranh giới nguyên tử hoặc effect idempotent | idempotency.py:41-66 (có ghi chú thiết kế); `test_idempotency.py` chỉ test replay và in-flight | **Chưa đáp ứng** ở cửa sổ crash | Crash sau `run()` commit, trước complete: key kẹt IN_PROGRESS 24 giờ, agent sinh key mới, chỉ còn version check chặn trùng. Contention ở `_complete` khiến MCP retry báo lỗi dù effect đã commit. Sửa: ghi record COMPLETED trong cùng transaction với effect |
| 06-DD1 (quan sát repo) Hai lần quan sát cùng SHA? | CAS chỉ trên `observed_remote_refs` | Như trên | planning.py:310-317; `local_commit` đọc trước fetch (git_worktrees.py:79 vs 109) | **Chưa đáp ứng** | Merge hoàn tất trong lúc fetch → sync ghi lại SHA cũ, mở reconciliation rỗng, lần sau mở thêm một lần cho SHA đã merge. Mỗi reconciliation buộc Planner phân loại mọi ticket mở |

### 3.2 Tranh chấp tài nguyên (chương 07)

| ID | Câu trả lời của HoH | Tiêu chí nguồn | Evidence | Kết luận | Khoảng thiếu |
| --- | --- | --- | --- | --- | --- |
| 07-DD1 Retry cùng request vs hai request khác tranh suất cuối? | Retry: idempotency key. Tranh chấp: partial unique index một lease/ticket + `_claim` trong `BEGIN IMMEDIATE` | PDF 206–224: unique ID chữa retry; guarded write bảo vệ tài nguyên | store.py:427; leases.py:252-339; `test_postgres_concurrency.py:9` | Đã kiểm chứng | Các `UPDATE tickets` (28 chỗ) không có predicate status/version; đúng chỉ nhờ serialize toàn cục. Chuyển PostgreSQL sang row lock, hoặc thêm writer không `immediate`, sẽ biến chúng thành lost update im lặng |
| 07-TĐ4 Concurrency quan trọng nhất ở đâu? | Một writer toàn cục cho mọi thao tác | PDF 199–200: xác định điểm nóng tranh chấp | SQLite `BEGIN IMMEDIATE`; PostgreSQL một advisory lock/schema (postgres_store.py:171) | Đáp ứng ở mức thiết kế cho một host | Mọi write, kể cả ghi log `agent_tool_calls` sau mỗi MCP call, xếp hàng sau git verify trong `submit_candidate` đồng bộ (candidates.py:279-358) |

### 3.3 Side effect ngoài, ambiguity và reconciliation (chương 11, 12)

| ID | Câu trả lời của HoH | Tiêu chí nguồn | Evidence | Kết luận | Khoảng thiếu |
| --- | --- | --- | --- | --- | --- |
| 11-DD1 Timeout/crash khi gọi hệ thống ngoài có gây effect hai lần? | Merge queued: intent + recover theo SHA thật | PDF 324–336: durable identity, trạng thái pending/unknown, recovery | integration.py:1059-1066, 1163-1205; `test_operational_recovery.py`, `test_integration_async.py:499` | Đã kiểm chứng (đường queued) | Đường đồng bộ cũ `merge_candidate` (integration.py:88-200) fast-forward không lock, không intent, kiểm barrier **sau** khi Git đã đổi. Không MCP tool nào gọi nó nhưng nó vẫn public; nên xóa |
| 11-TĐ4 Đối chiếu sổ nội bộ với hệ thống ngoài? | Recover đối chiếu với Git; sync quan sát remote refs | PDF 316–317: reconciliation là bắt buộc, webhook không thay thế | Như trên | Đáp ứng ở mức thiết kế | Không có job reconciliation định kỳ toàn cục (ví dụ: ticket MERGE_READY mà commit đã nằm trên main). Phát hiện chỉ xảy ra khi có recover hoặc sync |
| 12-DD1 Giao dịch dở dang khi coordinator chết? | Job RUNNING được supervisor recover; non-merge bị fail, merge đi reconcile | PDF 348–358, 363–374: trạng thái trung gian phải bền và có đường bù | supervisor.py:117-140 | Đáp ứng ở mức thiết kế với supervised mode | Merge đã vào Git nhưng completion bị barrier chặn (integration.py:1107 vs 1241): ticket ở MERGE_READY, code đã trên main. Gate càng dài càng dễ xảy ra. Planner job IN_PROGRESS bị bỏ rơi không bao giờ đánh thức ai (lease hết hạn lazy, wait chỉ đếm READY — planner_jobs.py:944-959, event_wait.py:259) |
| 12-TĐ2 Có tái dựng được lịch sử trạng thái? | Không; events là ghi chú | PDF 342–343: reproducibility khác reconciliation | Xem 04-TĐ2 | Chưa đáp ứng, nếu đặt yêu cầu này | Quyết định cần chủ dự án: HoH có cần chứng minh "vì sao ticket ở trạng thái này" từ log không? Nếu có, cần event payload đầy đủ và projection test |

### 3.4 Khả năng chịu tải

HoH chạy trên một host, một writer, vài agent. Không có mục tiêu throughput nào
được ghi. Theo sách, ước lượng phải đi từ workload thật: số agent, số write mỗi
phút, độ dài gate. Hiện chưa có số đo. Nút thắt quan sát được là thời gian vòng
lặp ticket, không phải QPS của DB. PostgreSQL không đem lại concurrency vì vẫn
một advisory lock toàn cục, file lock và supervisor đều gắn một host.
Kết luận: **chưa đủ bằng chứng**; việc cần làm là đo cycle time (xem §6, H1).

## 4. Đối chiếu harness cho agent

### 4.1 Năm hệ thống con

| Hệ thống con | Hiện trạng | Vấn đề có evidence |
| --- | --- | --- |
| Instructions | Role skill dạng control card + references có điều kiện (0.3.19); test kiểm tên tool tồn tại, giới hạn kích thước, link | AGENTS.md là file cục bộ (bị loại trong `.git/info/exclude`): agent làm ticket HoH trong worktree **không** nhận được nó. Bản cục bộ đã thành nhật ký release 1.426 từ, ghi bản cài là 0.3.23 trong khi host chạy 0.3.25, kèm trạng thái ticket Legal từ 0.3.12. Giao thức wait (12 giờ, 86400, resume_wait_id) lặp ở 5 nơi; luật barrier lặp ở 4 nơi |
| Tools (ACI) | 71 tool; Planner 51, Developer 24, QA 23, Integrator 21 | Docstring/skill nói `PASS`/`FAIL`, server nhận `passed`/`failed` (mcp_server.py:1776, qa.py:630) → lỗi chỉ lộ khi chạy. Schema MCP lấy từ chữ ký Python: không enum, không bound, `findings: list[dict]`. REST có model riêng với giới hạn khác service (2000/4000 vs 600). `capabilities()` quảng cáo `work:inbox` cho Planner, nhưng Planner không có tool đó. MCP bỏ `details` của lỗi (mcp_server.py:230), nên agent mất `missing_criteria`, `reason`, `contract_estimated_tokens` |
| Environment | `bootstrap-local.ps1`; desktop host | Ba interpreter sai, mỗi cái báo một lỗi khác nhau; editable install của `.venv-hoh` trỏ vào worktree cũ, nên pytest đã từng "pass" trên code cũ. `HOH_WORKER_MODE` mặc định `legacy` (không lock, không recover) |
| State | DB là nguồn sự thật; packet có snapshot | Quy tắc `context_snapshot_id` chỉ nằm trong shim MCP; REST bỏ qua nó. Claim tăng version nhưng không trả snapshot mới → mỗi claim buộc đọc lại cả packet ~4k token. Không có reply "not modified" cho packet không đổi |
| Feedback | 69 file test, gate coverage 80% | `main` không xanh (xem §5). Một suite đỏ sẵn làm agent quen bỏ qua failure. Không có metric outcome: cycle time, số lần chạy test, token mỗi ticket |

### 4.2 Fresh Session Test (L03)

| Câu hỏi | Trước | Sau nhánh này |
| --- | --- | --- |
| Hệ thống là gì? | README, spec 56 KB | README + ARCHITECTURE |
| Tổ chức ra sao? | Không có bản đồ module | ARCHITECTURE: layer, hướng phụ thuộc, mixin, nơi enforce bất biến |
| Chạy thế nào? | LOCAL_SETUP; README ghi `python -m pytest` (sai) | `scripts/test.ps1`; guard trong conftest |
| Xác minh bằng cách nào? | Phải biết mẹo PYTHONPATH từ memory | Script + guard; baseline ghi trong review |
| Tiến độ ở đâu? | AGENTS.md cache trạng thái cũ | Router chỉ ra: trạng thái ticket chỉ có qua MCP |

### 4.3 Context engineering (chương 2)

- Ngân sách ước lượng bằng `len(json.dumps(compact)) // 4`, nhưng MCP gửi
  `indent=2` cộng `structuredContent`. Kích thước thật vượt ước lượng; chưa đo
  vượt bao nhiêu. Nên đo trên serialization thật.
- Phần bị cắt vì ngân sách cite `ctx:ticket:<id>:vN:<section>`, mà
  `context.resolve` chỉ nhận `history` (context.py:976 vs 461). Rollup cite
  `ctx:ticket:<id>` cũng không resolve được. Role thực thi nhận reference
  `history` nhưng bị từ chối khi resolve. CONTEXT_DISTRIBUTION_REVIEW ghi lỗi
  này "fixed in 0.3.20"; thực tế mới sửa một phần. Cần test: mọi reference phát
  ra đều resolve được cho role đó.
- `work.discover` gửi lại toàn bộ project row và danh sách capability mà
  bootstrap đã trả.
- Experience note: không hết hạn, không provenance SHA, xếp hạng ưu tiên note
  cũ được nhắc nhiều → note lỗi thời tồn tại lâu.

### 4.4 Multi-agent và vòng lặp (chương 10, L13)

- Tách vai trò đúng mục đích (maker-checker độc lập), nhưng chi phí phối hợp
  chưa được đo so với lợi ích. Quan sát ngày 2026-09-26: QA tìm được bug thật
  (reassignment im lặng, mất text import, task đã hủy vẫn được đếm), nhưng kết
  luận của QA chỉ mang tính tư vấn, không chặn merge, trong khi chi phí là khoảng
  30 phút mỗi ticket.
- Fast-forward-only là một sequencing domain toàn cục trên `main`: các ticket phụ
  thuộc nhau land từng cái một, mỗi lần land mở một reconciliation buộc Planner
  duyệt mọi ticket mở. Chi phí reconciliation tăng theo số ticket mở × số commit.
- Classifier của client chặn một session vừa viết vừa merge (Self-Approval). Đây
  là ràng buộc đúng về an toàn, nhưng setup hiện tại không nói trước cho người
  vận hành.
- Deploy prompt cho claude-code/codex viết tay, mâu thuẫn với skill: chỉ đăng ký
  skill role, poll mỗi phút thay vì `work.wait_for_event`, Codex
  `tool_timeout_sec = 60` trong khi wait cần 86400. Bộ sinh thật là
  `agent_kit.py` và `local_agent.py`.

### 4.5 Cấu trúc code

- `ControlPlaneService` = 17 mixin. Import graph trông sạch (mỗi mixin chỉ import
  `_common`), nhưng các mixin gọi nhau qua `self`: khoảng 156 lần dùng method
  chéo module; `validation` và `leases` là kernel ẩn. Đọc một mixin không cho
  biết mọi luật áp dụng.
- `planning.py` 3.241 dòng gồm 8 cụm trách nhiệm; `mcp_server.py` 1.996 dòng
  (Planner chiếm 36%); `store.py` trộn seam kết nối, DDL 51 bảng, migration và
  side effect UDP.
- Không có repository layer: khoảng 450 lệnh `execute` inline rải trong mixin;
  luật state machine nằm cạnh SQL.
- Không có schema version: binary cũ chạy trên DB mới không bị chặn.

## 5. Baseline test

Chạy `main` `2fa75f3` bằng `.venv-hoh`, `PYTHONPATH` trỏ `src/` của checkout,
`--no-cov`: **824 passed, 34 failed, 30 skipped trong 17 phút 37 giây**. Nhánh
này: 939 passed, 34 failed, 30 skipped. Tập failure giống hệt `main`, và 115 test
mới đều pass.

Các failure có sẵn tập trung ở compaction/packet shape (7), recovery candidate
và QA retry (4), git worktree rework/takeover (4), integration gate/async (5),
repository sync (2), MCP flow (2), cùng một số test lifecycle và success
criteria. `test_repository_sync.py::test_a_remote_update_to_the_ticket_branch_stales_only_its_snapshot`
khớp với phát hiện của audit: `_claim` ném lỗi barrier trước khi tới kiểm tra
snapshot mà test chờ. `test_skill_assets.py::test_skills_only_name_registered_mcp_tools`
fail vì skill Integrator nhắc chuỗi `planning_barrier.repository_reconciliation_required`
mà regex hiểu là tên tool.

Hai điểm này là vấn đề harness tự thân:

- **Suite đỏ có sẵn.** 34 failure đã được ghi là "baseline" từ ngày 2026-09-17.
  Agent phải so sánh bằng tay với `main` mới biết thay đổi của mình có làm hỏng gì
  không. Đây là phép kiểm bị vô hiệu hóa: phải sửa hoặc đánh dấu `xfail` có lý do
  và ticket, để `main` xanh trở lại.
- **Một vòng phản hồi mất khoảng 18 phút.** Nếu QA chạy lại toàn suite ba lần cho
  mỗi ticket, riêng test đã gần một giờ máy. Cần tách suite nhanh (unit,
  contract) khỏi suite chậm (git/subprocess), và chọn test theo write scope.

## 6. Lộ trình đề xuất

Mỗi mục là một ticket HoH tiềm năng: vấn đề → sửa → phép kiểm. Thứ tự theo
correctness trước, chi phí vòng lặp sau, tái cấu trúc cuối cùng.

### P0 — correctness

| # | Vấn đề | Sửa | Kiểm chứng |
| --- | --- | --- | --- |
| C1 | Mất wake-up sau barrier | Khóa delivery theo `offer_id`; chỉ dedup delivery còn pending | Test: offer → ack → barrier → resolve → wait trả work |
| C2 | Merge vào Git, completion bị barrier chặn | Sau fast-forward, completion luôn ghi LANDED (Git là sự thật) và mở reconciliation kế tiếp; không kiểm barrier ở bước này | Test: mở barrier trong lúc gate chạy → ticket ở trạng thái landed + reconciliation mới |
| C3 | Git trong write lock (`submit_candidate` đồng bộ, REST) | REST dùng đường queued; hoặc verify git trước, re-check trong transaction ngắn | Test: verify git chậm (mock sleep) không chặn heartbeat |
| C4 | Writer không `immediate` | `create_project/feature/ticket` dùng `connection(immediate=True)`; thêm lint test cấm write qua connection không immediate | Test: tạo ticket READY trong khi sync mở reconciliation |
| C5 | Snapshot rule chỉ ở MCP | Bắt buộc `context_snapshot_id` trong chữ ký service; REST nhận field này | Test parity REST/MCP |
| C6 | Cửa sổ crash của idempotency | Ghi record COMPLETED trong cùng transaction với effect; reclaim IN_PROGRESS quá hạn | Test crash giữa run và complete |
| C7 | `LIKE '%ref%'` làm bằng chứng phân quyền | Cột/bảng tham chiếu có cấu trúc thay cho dò chuỗi payload | Test `x_1000` vs `x_10000` |
| C8 | Lost update khi quan sát repo | CAS gồm `observed_mainline_commit`; đọc local commit sau fetch | Test merge trong lúc fetch |
| C9 | Đường đồng bộ `merge_candidate` cũ | Xóa, chuyển test sang đường queued | Suite |
| C10 | Planner job bị bỏ rơi | Sweep lease Planner job trong scheduler; wait đếm job hết hạn | Test |

### P1 — giao diện agent và chi phí vòng lặp

| # | Vấn đề | Sửa | Kiểm chứng |
| --- | --- | --- | --- |
| H1 | Không đo outcome | Ghi cycle time theo phase (claim→submit→QA→merge), số lần chạy test, token packet đã giao mỗi ticket; hiển thị trên board | So trước/sau trên cùng loại ticket |
| H2 | `PASS` vs `passed`, schema lỏng | Một module contract pydantic dùng chung cho MCP và REST: `Literal`, `FindingInput`, `max_length` khớp hằng số service | Test sinh schema; test skill-text dùng enum thật |
| H3 | MCP bỏ `details` của lỗi | Trả `{code, message, details, retry_hint}` | Mở rộng `test_contract_parity` |
| H4 | Reference không resolve được | Chỉ phát reference role đó resolve được, hoặc mở rộng resolver | Test: mọi reference trong packet resolve được |
| H5 | Packet gửi lại toàn bộ | `known_snapshot_id` → `{unchanged: true}`; claim trả snapshot mới | Đo token trước/sau |
| H6 | Ngân sách đo sai serialization | Ước lượng trên payload thật đã gửi | Test so ước lượng với kích thước thật |
| H7 | Test chạy ba lần mỗi ticket | Tái dùng kết quả Developer khi cùng commit, cùng lệnh, cùng môi trường đã attest; QA chạy scenario độc lập thay vì chạy lại toàn suite | Đo số lần chạy và thời gian |
| H8 | Reconciliation đắt khi nhiều ticket mở | Tự phân loại `keep` cho ticket có write scope không giao với diff; Planner chỉ duyệt phần giao | Test manifest; đo số quyết định mỗi barrier |
| H9 | Deploy prompt viết tay, mâu thuẫn | Sinh `deploy/` từ `agent_kit`, hoặc xóa và trỏ về kit | Test so file sinh ra với file commit |
| H10 | Giao thức wait lặp 5 nơi | Một nguồn (mô tả tool), các nơi khác link | Test skill không chứa hằng số trùng |
| H11 | Experience không hết hạn | Provenance commit SHA, `retire`, hết hạn khi file scope đổi | Test |
| H12 | Scope `dir/` không bao giờ khớp (HOH-279) | Từ chối hoặc chuẩn hóa thành `dir/**` lúc cấu hình | Test |
| H13 | `main` có 34 test đỏ | Sửa từng nhóm, hoặc `xfail(strict=True)` kèm lý do và ticket; từ đó `main` phải xanh | Suite xanh trên `main` |
| H14 | Suite mất khoảng 18 phút | Marker `slow` cho test git/subprocess; suite nhanh chạy trước; QA chọn test theo write scope | Đo thời gian suite nhanh |

### P2 — cấu trúc, làm sau P0 và theo từng ticket

1. **Tách `mcp_server.py`**: `mcp/invoke.py` (audit, idempotency, map lỗi,
   retry), `mcp/workers.py` (một hàm spawn thay bốn bản gần giống), `mcp/tools/`
   theo role với `register(server, service, actor, invoke)`, `mcp/http_app.py`.
   Thành viên role là một bảng khai báo, đồng thời sinh `capabilities()`.
2. **Tách `planning.py`** theo 8 cụm: `project_setup`,
   `repository_reconciliation` (sở hữu luôn một `ExecutionGate` dùng chung cho
   claim, offer, READY), `project_policy`, `ticket_authoring`,
   `ticket_retirement`, `candidate_recovery`, `requirements`, `planner_context`.
3. **Từ mixin sang component tường minh**: bắt đầu với `ExecutionGate` (barrier)
   và `LeaseManager`, truyền vào bằng constructor, để phụ thuộc lộ ra trong code
   và test boundary có thể kiểm.
4. **Repository layer cho bảng `tickets`**: một chỗ thực hiện transition có
   predicate `WHERE status = ? AND version = ?`; rồi thêm CHECK cho `tickets.status`.
5. **Schema version** trong DB; binary cũ hơn từ chối khởi động.
6. **Tách `store.py`**: `store/connection.py`, `store/schema.sql` (hoặc module
   DDL), `store/migrations.py`; chuyển UDP hint sang commit hook của `event_wait`.

Mỗi bước refactor phải giữ nguyên tập test pass/fail so với baseline và đi qua
`tests/test_architecture_boundaries.py`. Không gộp refactor với thay đổi hành vi
trong một candidate: luật một-commit-mỗi-candidate của HoH vốn khuyến khích điều này.

## 7. Những gì nhánh `claude/harness-context-reorg` đã làm

| Thay đổi | Mục đích |
| --- | --- |
| `docs/README.md` | Chỉ mục mọi tài liệu, điều kiện "đọc khi", trạng thái current/snapshot |
| `docs/ARCHITECTURE.md` | Bản đồ layer, hướng phụ thuộc, mixin, nơi enforce bất biến, change routing |
| `docs/LOCAL_RUNTIME.md` | Sự thật về host tách khỏi AGENTS.md, kèm lệnh kiểm thay vì ghi cứng version |
| `docs/releases/` + index | Release note là lịch sử; index nói release nào canh bất biến nào |
| `scripts/test.ps1` | Một cách chạy test được hỗ trợ |
| `tests/conftest.py` guard | Từ chối chạy khi package import không thuộc checkout đang test |
| `tests/test_repo_docs.py` | Link tương đối phải tồn tại; AGENTS.md ≤ 900 từ; mọi doc có trong index |
| `tests/test_architecture_boundaries.py` | Khóa hướng phụ thuộc giữa layer; ngoại lệ đã biết được liệt kê |
| README | Sửa lệnh test sai và bảng tool lỗi thời |
| AGENTS.md (nay được git theo dõi) | Router ~800 từ thay nhật ký 1.426 từ cục bộ; ticket worktree cũng nhận được; bản cũ sao lưu ở `tmp/AGENTS.md.before-router-2026-10-04` |

Không đổi hành vi runtime nào, không đổi skill đã cài, không chạm DB hay service.
