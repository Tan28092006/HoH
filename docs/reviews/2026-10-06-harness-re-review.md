# Rà soát lại harness HoH lần 3 — 2026-10-06

Snapshot `main` `058bd4a` (0.3.33 + HOH-280). Đây là bản snapshot: số dòng đúng tại
commit này. Lăng kính: skill `ai-agent-build`, gồm *AI Agents in Depth* (Bojie Li) và
WalkingLab *Learn Harness Engineering* (L01–L14). Bản trước dùng lăng kính này là
`docs/reviews/2026-10-harness-architecture-review.md` (0.3.25). Bản system design
`2026-10-05-system-design-review.md` (0.3.30) dùng sách Alex Xu.

## 1. Phương pháp và giới hạn

**Code.** Đọc toàn bộ 26.079 dòng `src/hoh_control_plane` và khảo sát 91 file test
(25.034 dòng). Việc đọc chia thành bảy phần chạy song song, mỗi phần đọc trọn file của mình:

- lifecycle (leases, candidates, qa, integration, supervisor, git);
- coordination (discovery, planner_jobs, reconciliation, event_wait…);
- state/permission (store, migrations, api, auth, client setup);
- ACI/context (mcp, contracts, context, skills);
- docs/environment;
- test suite;
- đối chiếu G1–G16.

Các finding mức P0 được kiểm lại trực tiếp trong code; chúng có dấu **✔** kèm dòng
dẫn chứng. Finding không có ✔ là kết quả đọc code của phần tương ứng, chưa kiểm lần hai.

**Nguồn sách đã mở** (qua `read_source.py`, kiểm SHA-256):

| Tiêu chí | Locator |
| --- | --- |
| Fresh Session Test; knowledge decay; cập nhật tài liệu cùng code | L03 `index.md:39,64,75` |
| Instruction bloat; mỗi rule có nguồn, điều kiện, hạn | L04 `index.md:32-36,111` |
| Passing phải gắn bằng chứng; fail giữ active | L08 `index.md:48-52` |
| Termination criteria; Verification–Validation dual gate | L09 `index.md:43-44` |
| Bốn chi phí thầm lặng của vòng lặp | L13 `index.md:156` |
| Ranh giới tool quan trọng hơn khả năng; ví dụ gọi; độ trung thực tham số; chi phí định nghĩa tool | ch04 `78, 84, 90-98, 58, 126` |
| Pass@k và Pass^k | ch06 `256-263` |
| Lỗi Byzantine; cô lập worktree; xác thực chéo độc lập; vòng lặp mất kiểm soát | ch10 `549, 569, 590, 592` |

**Không chạy test** (yêu cầu của chủ dự án). Trước khi có yêu cầu đó, một lần chạy
`scripts/test.ps1` đã dừng giữa chừng với xdist `INTERNALERROR`
(`OSError [Errno 22]` khi flush stdout), sau nhiều `F/E`. Lần chạy này **không kết luận
được**: không thể dùng nó làm bằng chứng `main` đỏ hay xanh. Giả thuyết nguyên nhân ở mục E5.
MCP của HoH không kết nối được (ECONNREFUSED), nên review này không có số liệu live.

## 2. Tóm tắt

**Tiến bộ thật kể từ 0.3.25.** Gần hết C1–C10 và H1–H14 đã đóng; G2, G4, G5, G12 cũng
đóng. Các điểm đã ổn định:

- Lease là fencing token.
- Git không chạy trong write lock.
- Merge intent được ghi trước khi chạy Git.
- Có snapshot rule ở service.
- Idempotency theo `record_id` + `correlation_id`.
- Barrier được phân vùng theo ticket.
- Mọi `UPDATE tickets` đi qua `_transition_ticket`, có CHECK và event replay được.
- Packet có ước lượng token khớp với text gửi đi (`test_mcp_delivery.py:29`).
- `worker_brief` dưới 400 token.
- QA chạy trên snapshot sạch của SHA đóng băng.
- Mọi subprocess đều có timeout và bị kill cả cây.

Lõi concurrency giờ vững hơn nhiều so với lớp xung quanh nó.

**Nút thắt đã dời chỗ.** Rủi ro lớn nhất không còn ở lõi lease/merge. Nó nằm ở ba nơi:

1. **Tính toàn vẹn của bằng chứng ở completion gate.** Một run kết thúc vì lỗi hạ tầng
   vẫn được dùng làm bằng chứng merge. Candidate tự cấu hình phép kiểm provenance của
   chính nó. Không có gì buộc người kiểm khác người làm. (L08, L09, ch10:590)
2. **Liveness của vòng phối hợp.** Có ticket không bao giờ tới lượt, có Planner không
   được đánh thức, có job đóng trong khi ticket vẫn BLOCKED. (L07 DAG, ch10:592)
3. **Môi trường và instructions dẫn agent vào sai chỗ.** Script "dev" ghi vào DB thật.
   Venv chạy code 0.3.11. AGENTS.md chứa số liệu mục và trỏ tới review cũ. (L03, L06)

**Tám việc nên làm trước** (chi tiết ở §3):

| # | Việc | Loại |
| --- | --- | --- |
| R1 | Run có `exit_code=0` nhưng `termination_reason` là lỗi hạ tầng không được tái dùng hoặc qua gate | Bằng chứng |
| R2 | Ticket phụ thuộc vào ticket đã retire phải có đường ra (Planner job hoặc nối lại cạnh) | Liveness |
| R3 | Wait của Planner không bị 50 job READY cũ chặn; `delivery_completed` không tồn đọng | Liveness |
| R4 | Server từ chối verdict/merge khi `agent_id` trùng người submit; bỏ token dạng literal khỏi config client | Maker-checker |
| R5 | Khởi động không tự migrate DB; script dev không trỏ vào DB thật | An toàn dữ liệu |
| R6 | Fast-forward không bị hủy giữa chừng; dùng `update-ref` CAS | Side effect |
| A1 | `ticket_decisions` có schema typed | ACI |
| A2 | Mọi ref server phát ra đều resolve được | ACI |

## 3. Findings theo mức ưu tiên

Định dạng: vấn đề → evidence → kịch bản lỗi → sửa → phép kiểm. Mỗi mục ghi tiêu chí nguồn.

### P0 — bằng chứng, liveness, an toàn

**R1 ✔ Run lỗi hạ tầng có `exit_code=0` được tính là passing.** (L08:50 "passing kèm
bằng chứng"; L09:43 termination criteria)

- Evidence:
  - [qa_runner.py:126-134](../../src/hoh_control_plane/qa_runner.py) gán
    `output_truncated` (cùng các lý do hạ tầng khác) nhưng giữ nguyên `exit_code`.
  - Bảng `qa_test_runs` không có cột lý do kết thúc ([qa.py:566-587](../../src/hoh_control_plane/service/qa.py)).
  - Job được ghi `COMPLETED` rồi mới bị park.
  - Ba truy vấn quyết định chỉ xét `exit_code = 0 AND timed_out = 0`: reuse ở
    [qa.py:312-324](../../src/hoh_control_plane/service/qa.py), `_reusable_qa_run` ở
    [integration.py:779-783](../../src/hoh_control_plane/service/integration.py), và gate ở
    [integration.py:437](../../src/hoh_control_plane/service/integration.py).
- Kịch bản:
  1. Suite in ra hơn 200 KB, run bị đánh `output_truncated` và ticket chuyển BLOCKED.
  2. Planner chạy `recover_candidate_for_qa`.
  3. Lần `qa.run_project_tests` kế tiếp trả `reused: true`.
  4. Merge đi `gate_mode="reused_qa_run"` và land, dù không có run sạch nào.
- Sửa: lưu `termination_reason` và `provenance_status`; cả bốn truy vấn đòi
  `termination_reason='passed'`.
- Kiểm: một run truncated có exit 0 không được reuse, và gate báo `regression_run_failed`.

**R2 ✔ Ticket phụ thuộc vào ticket đã retire không bao giờ ready.** (L07 dependency
DAG; AGENTS rule 6 "constraint thành dependency edge")

- Evidence:
  - [discovery.py:82-86](../../src/hoh_control_plane/service/discovery.py) và
    [leases.py:556-569](../../src/hoh_control_plane/service/leases.py) chỉ coi `DONE`
    là đã đáp ứng; `CANCELLED`/`SUPERSEDED` không bao giờ đáp ứng.
  - [ticket_retirement.py:620](../../src/hoh_control_plane/service/ticket_retirement.py)
    viết "stay blocked until a Planner rewires them". Đây là prose, chỉ đi kèm một event.
  - Rollup xếp các ticket này vào `dependency_blocked_count` và không giao cho role nào.
- Kịch bản: B phụ thuộc A; A bị supersede bởi A2. B không bao giờ được offer hay đánh
  thức, và board hiện B là "waiting".
- Sửa: khi retire, nếu có thay thế thì nối cạnh B→A2; nếu không thì mở Planner job cho
  từng dependent.
- Kiểm: supersede A, khẳng định B có Planner job hoặc phụ thuộc mới.

**R3 ✔ Planner ngừng được đánh thức khi tồn 50 job READY.** (ch04 async lifecycle; L13
event loop)

- Evidence:
  - [event_wait.py:302](../../src/hoh_control_plane/event_wait.py) dùng
    `status='READY' ORDER BY created_at LIMIT 50`.
  - Key đã deliver bị bỏ qua ([event_wait.py:461-463](../../src/hoh_control_plane/event_wait.py)).
  - Mỗi merge mở một job `delivery_completed` READY
    ([planner_jobs.py:570-628](../../src/hoh_control_plane/service/planner_jobs.py)), và
    không có gì tự đóng nó.
  - `discover_planner_jobs` trả mọi job mở, không giới hạn. Điều này vi phạm bất biến
    packet có giới hạn.
- Kịch bản: sau 50 merge mà report đã được đọc nhưng chưa resolve, một
  `developer_blocker` mức high không bao giờ tới Planner.
- Sửa: loại key đã deliver ngay trong SQL, hoặc phân trang bằng cursor; đóng delivery
  report khi ack; cap discovery khoảng 10 job kèm tổng số.
- Kiểm: 51 job READY, 50 đã deliver và ack, thêm một blocker; wait phải trả blocker.

**R4 ✔ Tách maker/checker không được enforce theo danh tính.** (ch10:590 xác thực chéo
"từ góc độ độc lập"; L13 maker-checker; ch28 permission-before-execution)

- Evidence:
  - Trong `qa.py`, `integration.py` và `leases.py` không có chỗ nào so `actor.agent_id`
    với `candidates.submitted_by`.
  - Token map REST cho phép một `agent_id` giữ nhiều role ([api.py:585-590](../../src/hoh_control_plane/api.py)).
  - Mọi bearer token nằm plaintext ở `config/agent-credentials.json`
    ([local_credentials.py:23-34](../../src/hoh_control_plane/local_credentials.py)).
    `chmod 0o600` không có tác dụng trên Windows.
  - Setup ghi literal `Authorization: Bearer <token>` vào `~/.claude.json` và
    `~/.codex/config.toml` ([local_agent.py:113-127](../../src/hoh_control_plane/local_agent.py)).
  - Token không bao giờ rotate.
- Kịch bản: một Developer có shell đọc `~/.claude.json`, lấy token QA và Integrator, rồi
  tự duyệt và merge candidate của mình. Lớp chặn duy nhất hiện nay là classifier phía
  client (memory "Self-approval block"), không nằm ở server.
- Sửa:
  - Từ chối QA claim, verdict và merge khi `actor.agent_id == submitted_by`.
  - Dùng `bearer_token_env_var` vốn đã có trong `agent_kit.py:41,48` thay cho literal.
  - Lưu `sha256(token)` thay cho token gốc.
- Kiểm: verdict từ chính người submit nhận 403; config sinh ra không chứa `Bearer <secret>`.

**R5 ✔ Khởi động component tự migrate DB thật; script dev trỏ vào DB thật.** (AGENTS
rule 1, 2; L06 "script có effects gì?")

- Evidence:
  - `SQLiteStore.initialize` luôn chạy các rebuild và `record_schema_version`
    ([store.py:233-285](../../src/hoh_control_plane/store.py)), từ mọi entry point, không
    backup.
  - Một build cũ sau đó từ chối DB ([store_migrations.py:68-76](../../src/hoh_control_plane/store_migrations.py)).
  - `start-dashboard.ps1:16-20` và `start-scheduler.ps1:16` đặt
    `HOH_DATABASE_PATH=<repo>\data\control-plane.db`, `HOH_LOCAL_MODE=1`,
    `HOH_SEED_DEMO=1`, port 8765. Đây đúng là DB và port của host đã cài
    (`D:/HoH-service/config/settings.json`).
  - README và LOCAL_SETUP hướng dẫn chạy các script này như "dev instance".
- Kịch bản:
  - Một checkout mới hơn chạy `hoh-mcp` trỏ vào DB thật. DB bị nâng lên schema mới, và
    service đã cài (bản cũ hơn) không khởi động được nữa.
  - Một phiên mới làm theo LOCAL_SETUP thì đụng port, hoặc thêm project `demo` vào DB thật
    trong chế độ tin header.
- Sửa:
  - Process phục vụ chỉ từ chối DB cũ hơn chứ không tự nâng; nâng cấp qua
    `hoh-ops migrate` có backup `sqlite3.backup()` trước.
  - Script dev mặc định dùng `tmp/dev/control-plane.db`, từ chối đường dẫn thật và tắt seed.
- Kiểm: DB v1 mở bằng `hoh-mcp` không có cờ thì raise và file không đổi.

**R6 Fast-forward của checkout dùng chung có thể bị hủy giữa chừng; G14 vẫn mở.**

- Evidence:
  - `git merge --ff-only` chạy qua `_git` với timeout 15 giây ([git_worktrees.py:20,760-767](../../src/hoh_control_plane/git_worktrees.py)).
  - Supervisor dừng hoặc lease hết hạn thì `revoked()` hủy merge, và `run_managed` kill cả cây.
  - Trong `src` không có `update-ref <new> <old>`.
- Kịch bản: merge chậm (antivirus trên Windows), hoặc Ctrl+C. Working tree nằm ở
  candidate trong khi ref vẫn ở base (hoặc còn sót `index.lock`). Job đứng RUNNING 2.100
  giây, và mọi merge sau đều báo "must be clean".
- Sửa: không cho hủy trong pha fast-forward; dời ref bằng
  `git update-ref refs/heads/<b> <cand> <base>` dưới lock, rồi đồng bộ working tree.
- Kiểm: fake git ngủ 20 giây kèm `stopping.set()`; ref và working tree vẫn nhất quán.

**R7 G1 còn một cửa sổ: candidate được land tay trong lúc gate chạy thì bị đưa về REWORK.**

- Evidence: `fast_forward_default_branch` trả `(False, current)`
  ([git_worktrees.py:763](../../src/hoh_control_plane/git_worktrees.py)). Sau đó
  `_complete_queued_merge(merged=False)` xử lý như `base_drift`
  ([integration.py:815, 943-1043](../../src/hoh_control_plane/service/integration.py)).
  Phép kiểm ancestry chỉ có ở nhánh recover (`:878`).
- Sửa: trước khi phân loại drift, hỏi `commit_is_on_branch(candidate)`.
- Kiểm: fast-forward tay `main` tới candidate trong lúc gate chạy; ticket không được về REWORK.

**R8 Candidate tự cấu hình phép kiểm provenance của chính nó.** (L09 "check có đúng
oracle không?")

- Evidence:
  - `provenance_modules` được đọc từ `pyproject.toml` của chính candidate
    ([qa_runner.py:237-244](../../src/hoh_control_plane/qa_runner.py)).
  - Lệnh không phải `python -m pytest` thì không có provenance.
  - Số test passed, skipped và collected được ghi lại nhưng không bị kiểm.
- Kịch bản: Developer có `pyproject.toml` hoặc `conftest.py` trong scope, rồi skip mọi
  test hoặc khai một module tầm thường. Suite exit 0 và gate qua.
- Sửa: đóng băng `provenance_modules` trong `candidate_test_profiles` (Planner sở hữu);
  từ chối run có 0 test passed, hoặc có số test collected lệch mạnh so với base.
- Kiểm: candidate đổi `provenance_modules` trong khi có package bị shadow thì nhận
  `artifact_mismatch`.

**R9 Verdict `passed` được nhận dù có finding `failed`, nên ticket kẹt ở MERGE_READY.**

- Evidence:
  - [qa.py:812-813, 897-903](../../src/hoh_control_plane/service/qa.py) chỉ đòi mỗi
    criterion có một finding `verified`.
  - Gate sau đó từ chối với `unverified_qa_finding`
    ([integration.py:401-405](../../src/hoh_control_plane/service/integration.py)).
  - Thêm nữa, ref `evidence://local/.../missing.json` được nhận là `CHECKSUM_CLAIMED`
    ([qa.py:210-213](../../src/hoh_control_plane/service/qa.py)).
- Sửa: `passed` đòi mọi finding đều `verified`; ref local phải tồn tại.

### P1 — giao diện agent (ACI) và context

Tiêu chí ch04:78, 84, 98: ranh giới, ví dụ gọi, độ trung thực tham số.

**A1 ✔ Payload của quyết định reconciliation không typed.**

- Evidence:
  - `ticket_decisions: list[dict[str, Any]]` ([mcp/tools/planner.py:107](../../src/hoh_control_plane/mcp/tools/planner.py)).
  - Docstring không nêu tên key.
  - Service đòi `ticket_id`, `disposition` (6 giá trị) và `reason`
    ([repository_reconciliation.py:734-744](../../src/hoh_control_plane/service/repository_reconciliation.py)).
  - Skill nêu 6 giá trị nhưng không nêu tên key.
- Hậu quả: mỗi lần đoán sai là một vòng `validation_error`, đúng vào lúc cả board đang
  đóng băng.
- Sửa: model `TicketDecision` trong `contracts.py` (`Literal`, `extra="forbid"`), kèm một
  ví dụ gọi trong docstring.

**A2 ✔ Ref trong Planner job không resolve được.**

- Evidence:
  - Planner job phát `candidate:<id>`, `context:<id>` và `finding:<id>`
    ([qa.py:1016-1020](../../src/hoh_control_plane/service/qa.py), cùng
    `planner_jobs.py:583` và `repository_sweep.py:219,305`).
  - `context.resolve` chỉ nhận dạng `ctx:entry:`, `ctx:finding:`,
    `ctx:candidate:<id>:{developer_claims,changed_paths}` ([context.py:518-529](../../src/hoh_control_plane/service/context.py)).
  - `experience:<id>` lại cần một tool khác mà skill không nêu tên.
  - `test_context_references_resolve.py` chỉ quét dạng `ctx:` trên một board không có
    finding hay job.
- Hậu quả: bước "inspect the linked findings" thất bại đúng lúc Planner phải quyết định
  rework.
- Sửa: phát đúng dạng `ctx:`; một test thu mọi ref trong mọi kết quả tool sau một lần
  QA reject và resolve từng ref.

**A3 Nhiều tham số enum vẫn là `str`.**

- Evidence:
  - Tham số enum vẫn là `str`: `failure_class`, `job_kind`, `awaiting_role`,
    `required_role`, `ticket_type`, `resolution`, `resume_role`, `target_status`,
    `entry_type`.
  - `skill_manifest` vẫn là `dict[str, Any]`.
  - Docstring lệch code: `work.heartbeat` ghi "Developer or QA" nhưng tool cũng đăng ký
    cho Integrator; `experience.record` nhận `ticket_id` nhưng chuyển tiếp thành `ticket_ref`.
- Sửa: `Literal` sinh từ chính tập giá trị service kiểm.
- Kiểm: mọi tham số mà service so với một tập cố định phải có `enum` trong schema.

**A4 Hình dạng lỗi chưa thống nhất.**

- Evidence:
  - `work.wait_for_event` bỏ `details` và `retry_hint`.
  - Đường lỗi DB báo `state_conflict` trong khi audit ghi `database_error`.
  - Exception bất ngờ lộ nguyên text ra client; chỉ REST có test chống lộ.
  - Lỗi validate của pydantic không có `[code]` và không được audit.
  - Không lỗi nào có `structuredContent`.
- Sửa: một bộ map lỗi duy nhất trả `isError` kèm `{code, message, details, retry_hint}`.

**A5 Dispatcher chưa mỏng; luật "một ticket một worker mới" vẫn chỉ là prose.** (ch10
cô lập context; L13 token explosion)

- Evidence:
  - Skill bảo resolve packet trước khi claim, nên chat sống lâu nhận tới 4k token packet
    cho mỗi ticket.
  - `work.claim` trả nguyên dòng `SELECT * FROM tickets`.
  - `report_usage` do agent tự khai; soft cap 5 ticket "never refuses".
- Sửa:
  - Dispatcher claim theo version của offer, chỉ nhận `{lease, worker_brief}`.
  - Thêm `project_id` vào brief, vì `work.report_failure` cần nó.
  - Cap kích thước kết quả claim.

**A6 Barrier che hết Planner job; READY có phụ thuộc chưa xong vẫn được offer.**

- Evidence:
  - Có reconciliation pending, kể cả loại scoped, thì wait của Planner chỉ trả barrier
    ([event_wait.py:241-292](../../src/hoh_control_plane/event_wait.py)).
  - `create_ticket(ready=True)` và `set_ticket_ready` không kiểm phụ thuộc
    ([ticket_authoring.py:140-225](../../src/hoh_control_plane/service/ticket_authoring.py)),
    và `_offer_queue` không lọc phụ thuộc.
  - Hai mươi ticket ưu tiên cao bị chặn phụ thuộc lấp đầy cửa sổ 20 offer của inbox,
    nên việc làm được ở ưu tiên thấp không tới tay Developer.
- Sửa: trả barrier kèm job mức high; dùng `_DEPENDENCY_UNMET` trong truy vấn offer, hoặc
  từ chối READY khi phụ thuộc chưa xong.

**A7 Lỗi bình thường bị gắn nhãn "infrastructure".**

- Evidence:
  - Barrier hoặc lease hết hạn trong lúc gate đã qua dẫn tới `fail_queued_merge`: ticket
    về BLOCKED, mọi lease bị xóa, ghi "integration infrastructure failure"
    ([integration.py:799-807, 1113-1152](../../src/hoh_control_plane/service/integration.py)).
  - `pyproject.toml` hỏng trong candidate ném `TOMLDecodeError`, lỗi này không bị bắt,
    nên bị park thành infra kèm "No source rework is requested".
  - "must be clean" xảy ra trước khi Git đổi gì, nhưng vẫn bị xử lý như một side effect
    chưa chắc chắn.
- Hậu quả: Planner nhận chẩn đoán sai lớp (L01 "failure thuộc lớp nào?").
- Sửa: mỗi loại gián đoạn có outcome riêng; lỗi tiền điều kiện có kiểu riêng, "nothing
  changed", và job fail sạch.

**A8 Card dùng chung còn mô tả barrier theo nghĩa cũ.**

`skills/hoh-control-plane/SKILL.md` (Hard stops) nói `repository_reconciliation_required`
đóng băng mọi offer, claim, submit và merge. Từ 0.3.32, barrier có thể chỉ khóa một ticket
và các ticket phụ thuộc nó. Server tính barrier đúng theo từng ticket
([repository_reconciliation.py:914](../../src/hoh_control_plane/service/repository_reconciliation.py)),
nên đây chỉ là chữ lỗi thời.

### P1 — vòng phối hợp và chi phí

**C1 Mỗi commit quan sát được bắt Planner quyết định cho mọi ticket mở.** (L13 bốn chi
phí; ch10:592 vòng lặp mất kiểm soát)

- Evidence:
  - Push lên bất kỳ nhánh remote nào cũng mở barrier
    ([repository_reconciliation.py:106-128,190](../../src/hoh_control_plane/service/repository_reconciliation.py)).
  - Barrier giữ mọi ticket mở, kể cả BACKLOG (`:232-248`).
  - Resolve tuần tự (`:875-902`); danh sách quyết định bị cap 500 (`:704`).
  - Một ticket có `file_scope` rỗng tắt auto-keep cho mọi merge (`:662-663`), và
    `create_ticket` cho phép scope rỗng.
- Kịch bản: K lần push nhánh dẫn tới N×K quyết định; hơn 500 ticket mở thì không resolve
  được nữa.
- Sửa:
  - Auto-keep các ticket BACKLOG hoặc chưa có lease/base.
  - Bắt buộc có scope trước READY.
  - Gộp các observation project-wide đang pending.
  - Nhánh khác default chỉ để thông tin.

**C2 Job đóng trong khi ticket vẫn BLOCKED.**

- Evidence:
  - `_resolve_capability_review_jobs` đóng job `capability_review:<agent>`.
  - Trước đó `_open_planner_job` có thể đã gộp một QA failure thật vào cùng job
    ([planner_jobs.py:68-105](../../src/hoh_control_plane/service/planner_jobs.py),
    [capability.py:273-296](../../src/hoh_control_plane/service/capability.py)).
- Kết quả: ticket BLOCKED, không còn job, không còn wake-up.
- Sửa: không gộp capability review với failure; không resolve failure job khi ticket còn
  BLOCKED hoặc REWORK.

**C3 Một máy QA hỏng chặn N ticket.**

- Evidence: failure được định tuyến theo từng ticket ([planner_jobs.py:303-343](../../src/hoh_control_plane/service/planner_jobs.py)),
  không nhóm theo `(machine_id, failure_class)`.
- Sửa: nhóm theo máy và thu hồi approval của máy, thay vì block từng ticket.

**C4 Các gap cũ còn mở.**

- G9: chưa có drift budget.
- G10: không có kênh báo người (toast/webhook, retry tới khi ack).
- G11: sweep **mở barrier toàn project** khi không có Planner
  ([repository_sweep.py:118](../../src/hoh_control_plane/service/repository_sweep.py)),
  trái với đề xuất "chỉ phát hiện"; kết hợp với G10 thì không ai biết project đang đóng băng.
- G3: chỉ hội tụ sau khoảng 35 phút cộng thao tác tay, và merge đã qua gate bị ghi là ungated.
- `planner_interventions` không bao giờ đóng khi job resolve, nên `dispatch_overview`
  dạy người vận hành phớt lờ nó.

### P1 — instructions và environment

Tiêu chí: L03 Fresh Session Test và knowledge decay; L04 lifecycle của rule; L06 readiness.

**E1 ✔ `.venv-hoh` chạy code 0.3.11.**

- Evidence:
  - `_editable_impl_hoh_control_plane.pth` trỏ tới `D:\HoH\tmp\planner-merge-handoff\src`;
    dist-info ghi 0.3.11 trong khi pyproject ghi 0.3.33.
  - `test.ps1` và guard trong conftest chỉ bảo vệ test; các console script `hoh-*` trong
    venv vẫn chạy code cũ.
  - Gốc lỗi: `bootstrap-local.ps1:63` chạy `pip install -e ${repoRoot}` từ bất kỳ worktree nào.
- Sửa: bootstrap từ chối checkout không phải main; cài lại từ `D:\HoH`.

**E2 Quick start thiếu supervisor.** `DEFAULT_WORKER_MODE='supervised'`
([mcp/workers.py:29](../../src/hoh_control_plane/mcp/workers.py)), nhưng README và
LOCAL_SETUP chỉ khởi động dashboard và scheduler, và không có `start-supervisor.ps1`.
Hậu quả: job queued không bao giờ chạy.

**E3 RUNTIME_OPERATIONS.md:65 trỏ supervisor vào `D:\HoH-service\data\control-plane.db`.**
LOCAL_RUNTIME.md:20-23 cấm đúng đường dẫn đó. Cùng file còn tiêu đề "Runner contract in
0.3.5" và câu "This release adds…".

**E4 AGENTS.md tự vi phạm luật của nó.**

- Dòng 6 cấm ghi trạng thái live, nhưng dòng 27 ghi "1167 passed, 31 skipped, coverage
  83% on 2026-10-05".
- Ghi "about 5 minutes", trong khi `test.ps1:28` ghi "4.3 min with 10 workers".
- Còn 25 từ dư so với cap 900.
- Hàng "structural risks" trỏ tới review 0.3.25, bản còn ghi `HOH_WORKER_MODE` mặc định
  `legacy` (sai từ 0.3.31). Review system design 0.3.30 không được dẫn tới.
- "The service enforces these" bao cả "keeps at most three current priorities", nhưng
  `src` không có cap nào; luật này chỉ là prose trong skill Planner. Đây là vi phạm rule 6.
- Sửa: thay số liệu bằng "chạy `main` chưa đổi để lấy baseline"; trỏ rủi ro về
  `docs/README.md`; hoặc enforce cap ưu tiên, hoặc chuyển dòng đó ra khỏi danh sách
  "enforced".

**E5 `test.ps1` là một nguồn flakiness tiềm năng.** (L06 "setup idempotent?";
L09 "test failure mới hay baseline?")

- `--basetemp=D:\.hoh-pytest` dùng chung cho mọi checkout và worktree, mà pytest xóa
  basetemp lúc khởi động. Hai lần chạy đồng thời (hai agent, hoặc worktree cạnh main) sẽ
  xóa repo của nhau.
- `$ErrorActionPreference="Stop"` cộng stderr bị redirect có thể dừng script giữa chừng,
  rồi worker xdist ghi vào pipe đã đóng và nhận `Errno 22`. Đây là giả thuyết phù hợp
  với lần chạy bị hủy, **chưa kiểm chứng**.
- `PYTHONUTF8` không được đặt.
- Test không xóa `HOH_DATABASE_URL`, nên shell có biến này sẽ đẩy dữ liệu seed của sáu
  worker vào một DB thật.
- `conftest.py:44` nâng timeout Git lên 600 giây, nên ngân sách 15 giây của production
  không bao giờ được thử.
- Sửa: basetemp theo `<checkout-hash>-<pid>`; tạm `Continue` quanh lệnh pytest; fixture
  autouse xóa `HOH_*`; ghi log ra file và in tóm tắt.

**E6 Index tài liệu.**

- `test_every_doc_is_indexed` dùng `glob`, không dùng `rglob`, nên bỏ qua `docs/reviews/`.
- `ARCHITECTURE.md:302` dẫn việc "đổi packet shape" sang một snapshot 0.3.23.
- `UI_QA.md`, `QUALITY_CONTRACTS.md` và `deploy/*/README.md` (thời 0.3.4) vẫn ghi "current".
- README gọi spec 7,8k từ là "the full architecture".
- ARCHITECTURE ghi "25 mixins" trong khi thực tế có 26.

### P1 — feedback và evaluation

Tiêu chí: ch06:256-263 Pass^k; L11 process observability; L09 dual gate.

**F1 Không có phép đo outcome của agent.**

- Chưa có pass^k (RA2 còn mở).
- Token mỗi ticket do agent tự khai, có thể bị cộng trùng (không dedupe), và không tính Planner.
- `barrier_shadow.agreement_rate` không được ai đọc: không ngưỡng, không gate.
- `cycle_metrics` chỉ tính khi có người hỏi; không có cảnh báo.
- `experience.times_seen` được mô tả là "lesson is not reaching people" nhưng chỉ dùng để
  xếp hạng; nó nên mở một Planner job để encode lesson vào platform.
- Sách cảnh báo chính điều này: "Pass@k sẽ che đậy sự bất ổn" (ch06:262).
- Sửa: một lane eval nhỏ, cùng ticket chạy k lần trên một fixture repo; đo pass^k, token
  và cycle time cho từng role.

**F2 Oracle còn lỗ.**

- Chỉ có claim/offer bị kiểm việc từ chối khi barrier mở; khoảng 13 lời gọi guard khác
  không có test.
- Không test nào submit candidate 2 commit hoặc merge commit, dù đây là bất biến "exactly
  one ordinary commit".
- `work.report_failure` không có test nào.
- `HostCapabilityProber` thật không bao giờ chạy, chỉ có FakeProber.
- Khoảng 300 dòng SQL dựng state bằng tay: metrics và release test chứng minh phép tính,
  không chứng minh lifecycle thật sự ghi ra những dòng đó.
- Postgres (21 test) và UI bị skip mặc định.
- Một số kiểm tra âm tính dựa vào đồng hồ (`test_managed_process.py:75-79`) thiên về pass
  khi máy tải nặng.

**F3 Không có fast lane.**

- Marker duy nhất là `ui`.
- Gate coverage nằm trong `addopts`, nên chạy test chọn lọc mà thiếu `--no-cov` sẽ trả
  non-zero dù mọi test pass.
- Test import lẫn nhau (`test_integration_async` bị khoảng 10 file import), nên không
  chọn test theo write scope được.

### P2 — độ bền và cấu trúc

- **Ghi log có thể biến thành công thành lỗi.** `record_tool_call` chạy sau khi lệnh đã
  commit, nằm ngoài try ([mcp/invoke.py:142-148](../../src/hoh_control_plane/mcp/invoke.py)).
  Nó có thể ném `ContentionError` lên một lệnh đã thành công. Nên làm best-effort.
- **Postgres crash khi đánh thức Planner.** `planner_wakeup.py:128` dùng
  `store.path.parent`, nhưng với Postgres thì `path` là `str`.
- **Installer ghi `~/.claude.json` không nguyên tử.** Không có lock, và để lại các file
  `.hoh-backup-*` chứa token trong thư mục home ([agent_assets/install.py:76-108](../../src/hoh_control_plane/agent_assets/install.py)).
- **Local mode của `hoh-api` tin header.** Nó cấp `admin` với project `*` cho bất kỳ
  `X-HOH-Role`, và bind theo `HOH_HOST` không giới hạn. Token REST không có độ dài tối
  thiểu và không reload được.
- **Luật chuyển trạng thái nằm rải rác.** Guard đã ở một chỗ, nhưng ma trận "từ trạng
  thái nào sang trạng thái nào" nằm ở 24 call site trong 10 module. Nên có bảng
  `ALLOWED_TRANSITIONS`, assert trong `_transition_ticket`.
- **Idempotency còn hai cạnh.** Reservation quá 600 giây mà chưa có event bị reclaim
  trong khi request gốc có thể vẫn đang chạy. Lệnh lỗi sau khi đã commit một phần vẫn
  được trả là `committed`.
- **Scheduler không có try/except** quanh `sweep_once`, nên một `ContentionError` làm chết
  process. Mặc định của nó là đường dẫn tương đối `data/control-plane.db`.
- **Candidate đóng băng không được ghim bằng ref.** Không có `refs/hoh/candidates/<id>`;
  force-push, `fetch --prune` rồi gc có thể làm mất commit của một ticket đang BLOCKED.
- **Đường dẫn không phải ASCII.** `git diff --name-only` thiếu `-z`, nên đường dẫn
  non-ASCII bị quote và rớt scope check. Patch recovery bị encode lại UTF-8.
- **Write scope được nhả lúc submit.** Ticket chồng scope được claim trong khi candidate
  đầu còn chờ QA, nên drift có thể đoán trước.
- **Experience mất provenance.** Khi lesson được củng cố, `source_commit` bị ghi đè. Lesson
  hết hạn theo thời gian, không theo việc file thay đổi.
- **Lint write-lock (G16)** vẫn chỉ quét `service/` và `event_wait.py`, và bỏ sót write đi
  qua helper.

## 4. Fresh Session Test (L03:39) lần nữa

| Câu hỏi | 0.3.25 | Hiện tại | Còn thiếu |
| --- | --- | --- | --- |
| Hệ thống là gì? | README, spec | README, ARCHITECTURE (map khớp code, đã kiểm 41 symbol) | README gọi spec là "full architecture" |
| Tổ chức ra sao? | Chưa có map | ARCHITECTURE sau đợt tách 0.3.30 | Số đếm sẽ lỗi thời ("25 mixins", "156 cross uses") |
| Chạy thế nào? | Sai lệnh | `test.ps1`, LOCAL_SETUP | **Script dev ghi vào DB thật; thiếu supervisor; venv chạy 0.3.11** (R5, E1, E2) |
| Kiểm bằng cách nào? | Mẹo PYTHONPATH | `test.ps1` + guard | Không có fast lane; basetemp dùng chung; số liệu "green" ghi cứng (E4, E5, F3) |
| Đang ở đâu? | AGENTS cache trạng thái | Chỉ qua MCP | Đúng thiết kế; khi MCP chết thì phiên mới không có gì, đây là giới hạn chấp nhận được |

Kết luận: câu hỏi 1, 2, 5 đạt. Câu 3 và 4 có câu trả lời, nhưng câu trả lời dẫn tới hành
động nguy hiểm hoặc sai. Theo L03:64, "tài liệu lệch khỏi code là kẻ thù lớn nhất, tệ hơn
cả việc không có tài liệu".

## 5. Trạng thái gap từ các review trước

| Gap | Trạng thái | Ghi chú |
| --- | --- | --- |
| G1 candidate đã trên main | Một phần | Có kiểm ở queue và trước gate; cửa sổ sau gate còn mở (R7) |
| G2 claim → snapshot | Đóng | `test_a_committed_claim_survives_contention_while_building_its_brief` |
| G3 merge land, ticket đổi | Một phần | Sweep + `close_stuck_merge_job`; mất provenance gate |
| G4 idempotency ABA | Đóng | `test_idempotency.py:386,441,473,513` |
| G5 barrier theo scope | Đóng | `tests/test_scoped_barrier.py` |
| G6 shadow | Một phần | Chỉ đếm false negative; không ai đọc `agreement_rate` |
| G7 merge lane | Gần đóng | Hàng đợi vẫn FIFO |
| G8 năng lực | Một phần | Không tính việc đang dở; không có utilization |
| G9 drift budget | Mở | — |
| G10 báo người | Mở | — |
| G11 Git sweep | Một phần | Không kiểm DONE còn reachable; sweep tự mở barrier |
| G12 wave4/5 | Đóng | — |
| G13 replay | Một phần | Rework counter vẫn đếm event |
| G14 FF CAS | Mở | R6 |
| G15 diễn tập backup/restore | Mở | `synchronous=NORMAL` |
| G16 lint write-lock | Mở | — |
| RA1 token mỗi ticket | Một phần | Tự khai, không dedupe |
| RA2 pass^k | Mở | F1 |
| RA3 reset budget | Mở (doc) | Một câu trong card Planner |
| RA4 dispatcher | Một phần | Chỉ đo, không enforce (A5) |

## 6. Encode constraints in the platform (rule 6): các luật còn là prose

| Luật | Nơi đang nằm | Nên thành |
| --- | --- | --- |
| Dependent của ticket đã retire phải được nối lại | Comment `ticket_retirement.py:620` | Cạnh phụ thuộc mới hoặc Planner job (R2) |
| Tối đa 3 ưu tiên | AGENTS.md, skill Planner | Kiểm trong `ticket_authoring`/`project_policy`, hoặc rút khỏi "enforced" |
| Người kiểm ≠ người làm | Classifier phía client | Kiểm `agent_id` ở server (R4) |
| Một ticket một worker mới | 5 chỗ prose | Claim trả brief, cap kích thước kết quả (A5) |
| Không khởi động khi thiếu `HOH_DATABASE_PATH` | AGENTS.md | `api.py:139` và `scheduler.py:55` từ chối mặc định tương đối |
| `report_failure` trước khi idle | Skill | Ít nhất một test; cân nhắc để wait trả lời nhắc |

## 7. Lộ trình đề xuất

Mỗi dòng là một ticket HoH tiềm năng. Không gộp refactor với thay đổi hành vi trong một
candidate. Trước khi nới bất biến nào, đọc release note gốc của nó (rule 7).

| Wave | Nội dung | Vì sao trước |
| --- | --- | --- |
| **8A — bằng chứng** | R1, R8, R9, R4 (kiểm `agent_id`), F2 (test candidate 2 commit, test barrier cho mọi guard) | Gate là thứ duy nhất phân biệt "đã xác minh" với "agent nói xong" (L09) |
| **8B — liveness** | R2, R3, A6, C2, A7, G11 chỉ phát hiện | Ticket kẹt im lặng là lỗi Byzantine của chính harness (ch10:549) |
| **8C — an toàn môi trường** | R5, E1, E2, E3, E5, token env-var | Một phiên mới làm đúng theo docs không được phá DB thật |
| **8D — ACI** | A1, A2, A3, A4, A8; docstring kèm ví dụ gọi (ch04:84) | Rẻ, giảm số vòng đoán sai của agent |
| **8E — side effect Git** | R6/G14, R7, ghim ref candidate | Cần thiết kế kỹ; đụng merge path |
| **8F — đo lường** | F1 (lane pass^k), dedupe usage, ngưỡng promote shadow, G10 | Trước khi tối ưu chi phí vòng lặp, phải đo được nó (ch06) |
| **Sau đó** | C1 (chi phí reconciliation), C3, P2 | Cần số liệu từ 8F để chọn cách |

## 8. Những gì chưa kiểm

- Không chạy test nào; mọi "test X kiểm Y" nghĩa là đã đọc test đó, chưa chạy.
- Không đọc DB live, không gọi MCP. Mọi nhận định về tần suất (bao nhiêu job tồn, bao
  nhiêu push nhánh) là suy ra từ cơ chế.
- Chưa đọc trọn `AGENT_KIT.md`, `ROLE_WAKEUPS.md`, `GUI_EVENT_WAIT.md`, `UI_QA.md`.
- Số tool theo role (Planner 55, Developer 25, QA 24, Integrator 22; 76 tên) đếm từ guard
  đăng ký, chưa chạy `list_tools`.
- Lần chạy suite bị hủy chưa được chẩn đoán; giả thuyết ở E5.
