# Rà soát lại HoH theo *System Design Interview Vol. 2* — 2026-10-05

Snapshot: `main` `43cd571` (0.3.30, gồm C1–C10, H1–H14 và đợt tách module), cùng hai
nhánh chưa merge `claude/wave4` (`06926c6`) và `claude/wave5` (`b594a08`, worktree
`tmp/wave5` sạch tại thời điểm đọc). Cả hai nhánh đều tách từ `43cd571`, độc lập với nhau.
Số dòng dẫn chứng đúng tại các commit này.

Đây là bản rà soát chỉ-đọc. Không chạy test suite, không mở `D:/HoH/data`, không chạm
`D:/HoH-service`. MCP của HoH không kết nối được trong phiên (ECONNREFUSED), nên không
có số liệu live (cycle metrics, thời gian barrier, tỉ lệ drift) — mọi con số dưới đây là
ước lượng hoặc lấy từ release note, không phải phép đo.

---

## 1. Phạm vi và phương pháp

**Nguồn sách.** Skill `system-design-alex-xu`: đọc `chapters/experience-checklist.md`, rồi
toàn văn guide 04, 05, 06, 07, 11, 12 (gồm phần *Experience/checklist*). Bản scan được
đọc bằng `scripts/read_pdf.py` (OCR local, kiểm SHA-256) qua Python 3.13 của máy (Python
mặc định thiếu PyMuPDF).

**Trang PDF đã thực sự mở** (trang vật lý của file scan):

| Chương | Đọc toàn trang | Chỉ dò theo từ khóa (`--query`/grep trên OCR) | Ghi chú OCR |
| --- | --- | --- | --- |
| 04 Message queue | 116, 118, 124, 127, 128 | 115, 121–122 | ổn |
| 05 Metrics/alerting | 156, 157 | 150–155 | ổn |
| 06 Ad click aggregation | 180–183, 185–186, 193–194 | 179, 184, 188–192 | PDF 186 bị cắt mép phải; câu "đặt bước 4–6 trong một distributed transaction" ghép lại được từ các dòng kế tiếp |
| 07 Hotel reservation | 200–201, 210–224 | — | Công thức ước lượng ở PDF 200 bị OCR hỏng; chỉ dùng *phương pháp* (funnel, đi ngược từ TPS cuối), không dùng con số. Hình 7.10–7.11 (PDF 213–214) hỏng nhưng văn bản giải thích đọc được. "118%" ở PDF 215 là OCR của hệ số overbooking, không ảnh hưởng kết luận |
| 11 Payment | 326–336 | 316–317 | PDF 327 có ký tự rác cuối trang, nội dung chính đọc được |
| 12 Digital wallet | 342–343, 351, 356–361 | 350, 353–356, 364–367, 371–373 | Bảng 12.3–12.5 hỏng một phần; chỉ dùng phần chữ |

Không đọc PDF 348–349 và các trang 362–374 ngoài phần dò từ khóa; các kết luận về TC/C,
Saga, phase status và event sourcing chỉ dựa trên những trang ghi ở trên.

**Code.** Đọc `docs/ARCHITECTURE.md`, review trước
(`docs/reviews/2026-10-harness-architecture-review.md`, §3 và §6), release note
0.3.26–0.3.30, rồi kiểm từng claim trong `src/hoh_control_plane` và `tests/`. Nhánh
wave4/wave5 đọc bằng `git diff`/`git grep`.

**Thang kết luận** (của skill): *đáp ứng ở mức thiết kế* · *đã kiểm chứng* (chỉ khi có test
được nêu tên) · *chưa đáp ứng* · *chưa đủ thông tin/bằng chứng* · *không áp dụng*.

**Ánh xạ khái niệm** (giữ từ review trước): lease/ticket ↔ "phòng cuối cùng" (ch07); Git ↔
PSP, hệ thống ngoài mà control plane phải đối chiếu (ch11); `merge_intents` ↔ phase status
table (ch12); context packet ↔ cache, write re-validate ↔ "DB kiểm cuối" (ch07);
`audit_events` ↔ log (ch04/ch12); `cycle_metrics` ↔ derived aggregate (ch06/ch05).

---

## 2. Bản ghi checklist theo chương

Định dạng mỗi bản ghi: **Câu hỏi → HoH thực sự làm gì → Tiêu chí nguồn (trang đã đọc) →
Evidence → Kết luận → Khoảng thiếu / kiểm chứng tiếp.**

### 2.1 Chương 04 — log, ordering, delivery

**04-TĐ3 · Ordering và delivery semantics có phạm vi rõ không?**
- HoH: `audit_events.sequence` là `INTEGER PRIMARY KEY AUTOINCREMENT`
  (`store_schema.py:705-719`), cấp phát trong write lock toàn cục; C4 buộc mọi write đi qua
  `connection(immediate=True)`. Wake-up là at-least-once có ack (`agent_event_deliveries`).
- Nguồn: PDF 116 (consumer commit offset rồi consumer mới đọc lại từ state), PDF 127–128
  (at-least-once: commit offset *sau* khi xử lý ⇒ có thể trùng; dedupe bằng unique key phía
  consumer).
- Evidence: `tests/test_architecture_boundaries.py:158`
  `test_every_write_takes_the_write_lock` (quét tĩnh `service/*.py` và `event_wait.py`);
  `test_event_wait.py:267` `test_barrier_rearms_a_wakeup_the_agent_already_acknowledged`.
- Kết luận: **đã kiểm chứng** trên SQLite; **đáp ứng ở mức thiết kế** trên PostgreSQL
  (`test_postgres_concurrency.py` chỉ chạy khi có `HOH_TEST_POSTGRES_DSN`; release
  0.3.28/0.3.29 ghi "PostgreSQL paths not run").
- Thiếu: lint chỉ quét `service/` và `event_wait.py`; `supervisor.py` hiện chỉ đọc qua
  connection thường nên đúng, nhưng không có gì giữ điều đó. Mở rộng lint ra toàn bộ
  `src/hoh_control_plane`.

**04-DD1a · Dedupe có làm mất tin hợp lệ không (wake-up sau barrier)?**
- HoH: resolve reconciliation xóa các delivery đã ack của queue item còn pending/offered
  (`repository_reconciliation.py:731-745`, gọi ở `:668`).
- Nguồn: PDF 127–128: dedupe là để chống trùng, không được nuốt tin chưa xử lý.
- Evidence: `test_event_wait.py:267`.
- Kết luận: **đã kiểm chứng** (C1 đóng).

**04-DD1b · Agent nhận "ACK" khi nào so với lúc dữ liệu bền?**
- HoH: một host, một SQLite WAL, `PRAGMA synchronous = NORMAL` (`store.py:231`): commit
  sống qua crash tiến trình nhưng có thể mất vài commit cuối khi mất điện/OS crash. Không
  có replica. Merge đối chiếu lại với Git khi recover; claim/lease mất thì agent bị từ chối
  ở write sau (fail-closed).
- Nguồn: PDF 118 (committed offset = đã đồng bộ tới mọi ISR), PDF 124 (min ISR: càng cao
  càng an toàn, đổi lấy latency; mọi replica chết thì mất dữ liệu).
- Evidence: `test_operational_recovery.py:63`
  `test_reconcile_merge_after_git_completed_and_worker_crashed` (Git đã đổi, DB chưa ghi
  completion → recover thành COMPLETED một lần).
- Kết luận: **đáp ứng ở mức thiết kế** cho mô hình một host; **chưa đủ bằng chứng** cho
  khôi phục: `docs/RUNTIME_OPERATIONS.md:148` vẫn ghi diễn tập backup/restore DB+Git+evidence
  với RPO/RTO đo được là việc còn mở.

**04-TĐ2 · Có replay được từ log không?** Chuyển sang Câu hỏi 5 (§3.5).

### 2.2 Chương 06 — crash giữa output và checkpoint, dedupe, reconciliation

**06-DD1a · Crash giữa effect và ghi kết quả idempotency.** Xem bản ghi đầy đủ ở Câu hỏi 4
(§3.4). Tóm tắt: **đáp ứng ở mức thiết kế, một phần đã kiểm chứng**; ranh giới vẫn không
nguyên tử và còn một lớp lỗi cùng loại ở lệnh nhiều transaction.

**06-DD1b · Hai lần quan sát repo ghi đè nhau (lost update).**
- HoH: đọc `local_commit` sau fetch; CAS gồm cả `observed_remote_refs` và
  `observed_mainline_commit`; lệch thì retry một lần (`repository_reconciliation.py:30-45`,
  `:172-186`).
- Nguồn: PDF 185–186: lưu checkpoint trước khi output được xác nhận gây mất dữ liệu; lưu
  sau thì gây trùng; chỉ ranh giới nguyên tử mới loại cả hai.
- Evidence: `test_repository_sync.py:257`
  `test_a_merge_landing_during_the_fetch_is_not_overwritten_or_reconciled_twice`.
- Kết luận: **đã kiểm chứng** (C8 đóng).

**06-DD1c · Ngưỡng "trễ" (watermark) cho trạng thái chưa biết.**
- HoH: hai ngưỡng kiểu watermark: IN_PROGRESS quá 600 s bị coi là bỏ rơi
  (`idempotency.py:35`); barrier leo thang ở 30 phút/2 h/8 h/24 h (`event_wait.py:45`).
- Nguồn: PDF 180–181: watermark dài bắt được sự kiện trễ hơn nhưng thêm latency; sự kiện
  trễ quá watermark phải để reconciliation cuối ngày sửa.
- Evidence: `test_idempotency.py:358`
  `test_a_crash_before_completion_is_reclaimed_as_unknown_after_the_stale_age`;
  `test_team_quality_gates.py:160`
  `test_an_ageing_barrier_escalates_and_says_how_long_work_has_been_blocked`.
- Kết luận: **đã kiểm chứng** cho hành vi ngưỡng.
- Thiếu: "sự kiện trễ" thật — request gốc hoàn tất *sau* khi key bị reclaim — được xử lý
  bằng `UPDATE ... WHERE <scope>` chứ không theo `record_id` (`idempotency.py:195-214`). Nếu
  đã có reservation mới cùng scope, completion muộn sẽ ghi đè nó, còn release muộn sẽ xóa
  nó. Cùng payload nên hậu quả nhỏ, nhưng đây là lỗi ABA; sửa bằng `record_id`.

**06 (PDF 193) · Đo latency giữa các stage và kích thước hàng đợi.**
- HoH: `planner.cycle_metrics` (`service/board.py:316-396`) tính các phase từ
  `audit_events`, chỉ cho ticket **đã giao**; `project_metrics`
  (`observability.py:80-99`) có thời gian chờ trung bình của work queue theo role và số
  merge theo `merge_status`.
- Nguồn: PDF 193: theo dõi timestamp ở từng stage và độ lớn hàng đợi (lag).
- Evidence: `test_cycle_metrics.py:91` `test_cycle_metrics_reports_phases_and_distribution`.
- Kết luận: **đã kiểm chứng** cho phép tính; **chưa đáp ứng** cho nhu cầu capacity: không đo
  thời gian chờ ở test slot (`qa_test_jobs.started_at - created_at`), thời gian barrier
  đóng (`repository_reconciliations.resolved_at - created_at`), tuổi của việc đang dở, và
  bị survivorship bias vì chỉ tính ticket đã DONE.

### 2.3 Chương 07 — tranh chấp tài nguyên cuối, guarded write, retry

**07-DD1 · Retry cùng request vs nhiều request tranh tài nguyên cuối.**
- HoH: (1) retry cùng request → idempotency key gắn hash payload; (2) tranh chấp → unique
  index `leases_one_per_ticket` (`store_schema.py:228`) là *database constraint*; kiểm
  chồng lấn write scope giữa các ticket khác nhau là check Python dưới write lock toàn cục
  (pessimistic, serialize); `expected_version` là optimistic check. Trên `main`, 25 lệnh
  `UPDATE tickets` không có predicate status/version.
  **wave4**: mọi ticket write đi qua `_transition_ticket`
  (`claude/wave4:service/_common.py:464-520`), `UPDATE ... WHERE ticket_id = ? AND version = ?
  AND status IN (...)` (`:502`), không khớp thì `ConflictError`; thêm `CHECK` cho
  `tickets.status`.
- Nguồn: PDF 210–212 (idempotency key = primary key của chính bản ghi reservation, nên
  dedupe và effect là một ràng buộc), PDF 214–215 (race khi check rồi update), PDF 215–216
  (pessimistic: dễ đúng, không scale, deadlock), PDF 217–218 (optimistic theo version:
  nhanh khi ít tranh chấp, sụp khi tranh chấp cao vì mỗi vòng chỉ một client thắng), PDF
  218–219 (DB constraint: tương tự optimistic).
- Evidence: `test_store_contention.py:112`
  `test_concurrent_developer_and_qa_activity_on_different_tickets_completes_cleanly`;
  `test_success_criteria.py:92` `test_criterion_3_overlapping_write_scope_is_serialised`;
  `test_postgres_concurrency.py:9` (gated). wave4: `tests/test_ticket_transitions.py`
  (`test_a_stale_version_is_refused_by_the_update_itself`,
  `test_a_service_write_interleaved_with_another_writer_is_refused`,
  `test_no_ticket_update_bypasses_the_guarded_transition`).
- Kết luận: `main` **đã kiểm chứng** trên SQLite nhờ serialize toàn cục; wave4 thêm một lớp
  conditional write đúng tinh thần PDF 217–219 → **đáp ứng ở mức thiết kế** cho trường hợp
  writer thoát lock (khi wave4 được merge).
- Thiếu: (a) chồng lấn scope là predicate trên không gian đường dẫn, không thể biểu diễn
  bằng row lock hay constraint; nó chỉ đúng chừng nào writer còn serialize. Ghi điều này vào
  ARCHITECTURE trước khi ai nghĩ tới row lock trên PostgreSQL. (b) `merge_jobs`,
  `qa_test_jobs`, `leases` vẫn dùng UPDATE không predicate. (c) wave5 thêm một raw
  `UPDATE tickets SET blocked_reason` (`claude/wave5:service/qa.py:925`), xung đột với
  `test_no_ticket_update_bypasses_the_guarded_transition` của wave4: khi gộp phải chuyển
  `REWORK_BUDGET_EXHAUSTED` vào `changes` của `_transition_ticket`.

**07-TĐ4 · Điểm nóng tranh chấp thật sự ở đâu?**
- HoH: "phòng cuối cùng" nóng nhất là ref `main`. Fast-forward-only là một optimistic
  version check trên `main`: chỉ merge khi `main == candidate.base_commit`
  (`git_worktrees.py:760-765`; drift ở `integration.py:673`). Mỗi lần land làm mọi
  candidate khác dựng trên base cũ thành drift → REWORK → Developer rebase → QA chạy lại toàn
  suite.
- Nguồn: PDF 217–218: dưới tranh chấp cao, mỗi vòng chỉ một client thắng, số còn lại retry;
  kết quả vẫn đúng nhưng lãng phí và trải nghiệm tệ. PDF 200 (TĐ): xác định concurrency là
  NFR chính dù TPS trung bình thấp.
- Evidence: `test_integration_async.py:271`
  `test_merge_worker_requeues_rework_when_mainline_drifts_after_queueing`;
  `test_integration_async.py:679` `test_drift_before_the_gate_skips_the_suite`.
- Kết luận: **chưa đủ bằng chứng** (chưa đo tỉ lệ drift), nhưng cơ chế cho thấy chi phí bậc
  hai: n ticket cùng qua QA trên một base, mỗi vòng land một cái, tệ nhất cần
  n + (n−1) + … + 1 = n(n+1)/2 lần chạy QA thay vì n. Xem Câu hỏi 3.

**07 (PDF 221–222) · Cache có được cấp quyền ghi không?**
- HoH: packet là bản "cache" của state; mọi write hệ quả phải nêu `context_snapshot_id`
  vừa đọc và được kiểm lại trong transaction ghi.
- Nguồn: PDF 222: cache lệch không sao, miễn DB làm phép kiểm cuối.
- Evidence: `test_context_snapshots.py:105`
  `test_a_claim_bound_to_context_the_world_moved_past_is_refused`;
  `test_context_snapshots.py:279`
  `test_the_service_refuses_a_consequential_write_without_a_snapshot`.
- Kết luận: **đã kiểm chứng** (C5 đóng, cả REST lẫn MCP).

**07 (PDF 223–224) · Nguyên tử khi nhiều bước.** Ticket, lease, queue, reconciliation,
intent và audit event nằm trong một DB và một transaction; chỉ Git ở ngoài. Đây đúng là
lựa chọn "hybrid, cùng một relational DB để dùng ACID" của sách. **Đáp ứng ở mức thiết
kế.** Bước nhiều đêm của sách tương ứng với lệnh nhiều bảng trong HoH; các lệnh đó đều nằm
trong một transaction, trừ những ngoại lệ ở §3.4.

### 2.4 Chương 11 — ambiguity với hệ thống ngoài (Git đóng vai PSP)

**11-DD1a · Timeout/crash quanh lệnh Git có gây effect hai lần không?**
- HoH: `merge_intents` được ghi cùng transaction với RUNNING (`integration.py:651-661`)
  trước khi chạy Git; Git chạy ngoài transaction; khi lỗi hoặc crash thì hỏi Git
  (`commit_is_on_branch`) và chỉ complete nếu có bằng chứng gate gắn với job
  (`_queued_gate_is_recorded`, `:764-772`); đường merge đồng bộ cũ đã bị xóa (C9).
- Nguồn: PDF 326 bước 4 (lưu token *trước* khi gọi trang PSP), PDF 334–335 (kịch bản 2: PSP
  đã xử lý nhưng response mất; dùng cùng key để PSP nhận ra), PDF 336 (dùng idempotency
  *và* reconciliation; đừng giả định hệ thống ngoài luôn đúng).
- Evidence: `test_operational_recovery.py:63`, `:83`
  `test_reconcile_pre_git_crash_does_not_merge_without_live_authorization`, `:93`
  `test_recovery_does_not_take_repository_lock_from_a_live_worker`;
  `test_integration_async.py:526`
  `test_a_second_worker_racing_the_same_job_never_runs_git_twice`, `:303`
  `test_duplicate_merge_request_reuses_one_job_and_merges_once`.
- Kết luận: **đã kiểm chứng**.
- Thiếu: fast-forward là check-then-act (`git_worktrees.py:760-767`: so `current` với
  `expected_base` rồi mới `merge --ff-only`), chỉ an toàn trước những writer cùng lấy
  `hoh-integration.lock`. Người vận hành commit tay vào checkout dùng chung không lấy lock
  đó. `git update-ref refs/heads/<b> <candidate> <base>` là CAS nguyên tử của chính Git
  (conditional write, PDF 218–219). Khi branch đang checkout, phải cập nhật cả working tree,
  nên cần kiểm tra cách làm này kỹ trước khi đổi.

**11-DD1b · Idempotency nội bộ sau C6.** Xem Câu hỏi 4.

**11-TĐ4 · Reconciliation với hệ thống ngoài có đủ không?** Xem Câu hỏi 6.

**11 (PDF 331–332) · Retry có giới hạn và được phân loại không?**
- HoH: MCP retry contention có giới hạn, backoff gấp đôi (`mcp/invoke.py:25-39`). Ở mức
  ticket, **wave5** thêm rework budget: lần QA reject thứ ba chuyển ticket sang BLOCKED cho
  Planner (`claude/wave5:service/_common.py:94`, `qa.py:694`); evidence gap được tách thành
  lỗi hạ tầng, không tính vào budget.
- Nguồn: PDF 331–332: retry queue cho lỗi retryable, dead-letter queue khi vượt ngưỡng; lỗi
  không retryable lưu riêng.
- Evidence: `claude/wave5:tests/test_rework_budget.py:56`
  `test_the_third_qa_rejection_blocks_the_ticket_for_planner`, `:79`
  `test_planner_resuming_the_ticket_records_the_reset`.
- Kết luận: **đáp ứng ở mức thiết kế** (trên wave5).
- Thiếu: budget chỉ đếm QA reject. Vòng base-drift → rework → QA → drift không có giới hạn,
  mà theo 07-TĐ4 đây chính là vòng dễ lặp nhất khi nhiều ticket chạy song song.

**11 (PDF 328–329) · Mismatch được phân loại và có hàng đợi xử lý không?**
- HoH: merge cần người xem được đánh dấu `error_message = 'Reconciliation required: ...'`
  và liệt kê trong `operations_status().needs_attention` (`supervisor.py:58`).
- Nguồn: PDF 328–329: ba loại mismatch: tự động sửa được; phân loại được nhưng phải sửa
  tay (đưa vào job queue); không phân loại được (queue điều tra riêng).
- Kết luận: **đáp ứng ở mức thiết kế** cho loại 2 và 3.
- Thiếu: hàng đợi này chỉ đọc theo kiểu pull (dashboard/API), không có ai được báo (xem
  05-DD1); loại 1 còn bỏ sót (§3.6).

### 2.5 Chương 12 — việc đang dở, coordinator crash, reproducibility

**12-DD1 · Giao dịch dở dang khi coordinator chết.**
- HoH: `merge_intents` đóng vai phase status table; supervisor quét job RUNNING khi khởi
  động và mỗi 5 s (`supervisor.py:117-140`, `:206-208`); merge thì đi reconcile, job khác
  thì fail kèm giữ artifact. C2: Git đã fast-forward thì completion ghi DONE dù barrier mở
  trong lúc gate (`integration.py:875-880`). C10: lease của Planner job hết hạn được sweep.
  `ticket_retirement_requests` là cờ "cancel đến trước", để merge đang chạy được đối chiếu
  khi recover (`integration.py:801-836`).
- Nguồn: PDF 351 (lưu tiến độ TC/C trong DB giao dịch để sống qua restart), PDF 353–354
  (Cancel đến trước Try: để lại cờ out-of-order), PDF 356 (Saga/TC/C là giao dịch ở tầng
  ứng dụng, compensation không phải rollback ACID).
- Evidence: `test_integration_async.py:567`
  `test_a_barrier_opened_during_the_merge_does_not_strand_a_landed_candidate`;
  `test_event_wait.py:316` `test_sweep_requeues_an_abandoned_planner_job`, `:324`
  `test_waiting_planner_is_woken_by_an_abandoned_planner_job`;
  `test_planner_work_queue.py:158`
  `test_an_expired_planner_lease_returns_the_job_to_the_ready_queue`;
  `test_operational_recovery.py:108`
  `test_recovery_retains_changed_ticket_for_operator_review`.
- Kết luận: **đã kiểm chứng** ở supervised mode. Trên `main`, `HOH_WORKER_MODE` mặc định
  vẫn là `legacy` (`mcp/workers.py:22-26`), tức không có job lock, timeout hay vòng recover.
  Desktop host đặt `supervised`; wave4 đổi mặc định
  (`claude/wave4:mcp/workers.py:29`, `test_supervised_is_the_default_worker_mode`).
- Thiếu: `test_recovery_retains_changed_ticket_for_operator_review` cho thấy một trạng thái
  kẹt có chủ đích: Git đã chứa candidate, nhưng version ticket đổi nên job ở RUNNING mãi,
  `integrator.record_external_landing` từ chối với `landing_job_active`
  (`integration.py:130-140`), và chỉ admin gỡ được. Xem Câu hỏi 6.

**12-TĐ2 · Có tái dựng được lịch sử không?** Xem Câu hỏi 5.

### 2.6 Chương 05 — metrics và leo thang barrier

**05-DD1 · Trạng thái cảnh báo, chống trùng, và "không có tín hiệu ≠ bình thường".**
- HoH: barrier tạo payload với `escalation_level` theo ngưỡng 1800/7200/28800/86400 s; key
  `repository-reconciliation:<id>:staleN` khiến mỗi mức chỉ phát một lần
  (`event_wait.py:226-262`). Người nhận là agent đang `work.wait_for_event`; với role thực
  thi, `next_action` bảo "nhờ operator mở Planner". Merge cần chú ý nằm trong
  `needs_attention`. Quan sát Git chỉ chạy khi có **Planner đang chờ**
  (`event_wait.py:129-150`).
- Nguồn: PDF 156: alert manager lưu trạng thái (inactive/pending/firing/resolved), gộp và
  khử trùng, retry tới khi thông báo được gửi ít nhất một lần qua kênh ngoài (email,
  PagerDuty, webhook).
- Evidence: `test_team_quality_gates.py:145`
  `test_a_fresh_barrier_tells_an_execution_role_to_hold`, `:160`, `:188`
  `test_the_inbox_reports_barrier_age_and_the_packet_stays_snapshot_stable`.
- Kết luận: **đã kiểm chứng** cho trạng thái và khử trùng; **chưa đáp ứng** cho việc phát
  tới người: không có kênh ngoài agent. Nếu không ai đang chờ, leo thang không đến được ai.
  Nếu không có Planner nào chờ, Git cũng không được quan sát.
- Kiểm chứng tiếp: test "không có waiter nào, barrier quá 2 h → một notification bền được
  ghi và gửi qua kênh cấu hình, retry tới khi ack".

**05-TĐ2 (chuyển thể) · Đo đúng đơn vị tải.** Đơn vị tải của HoH là lần chạy suite trên
host, không phải số ticket. Xem Câu hỏi 3.

---

## 3. Trả lời sáu câu hỏi

### 3.1 Sau C1–C10, mục "chưa đáp ứng" nào của review trước đã đạt, mục nào còn mở?

| Mục của review 2026-10-04 | Kết luận cũ | Nay | Bằng chứng |
| --- | --- | --- | --- |
| 04-DD1 mất wake-up sau barrier (C1) | chưa đáp ứng | **đã kiểm chứng** | `test_event_wait.py:267` |
| 06-DD1 cửa sổ crash idempotency (C6) | chưa đáp ứng | **đáp ứng một phần**: không còn khóa key 24 h; contention khi complete không che effect đã commit; key quá 600 s trả `idempotency_unknown`. Vẫn không nguyên tử, và lệnh nhiều transaction còn lỗi cùng loại (§3.4) | `test_idempotency.py:358`, `:410` `test_contention_on_completion_never_hides_a_committed_effect` |
| 06-DD1 lost update khi quan sát repo (C8) | chưa đáp ứng | **đã kiểm chứng** | `test_repository_sync.py:257` |
| 12-TĐ2 reproducibility | chưa đáp ứng (nếu đặt yêu cầu) | **vẫn mở, chủ dự án chưa chốt yêu cầu**; event nay còn bị dùng làm input cho quyết định (§3.5) | — |
| 04-TĐ3 thứ tự `sequence` trên PostgreSQL (C4) | gap | **đáp ứng ở mức thiết kế**; chưa chạy test PG | `test_architecture_boundaries.py:158` |
| 07-DD1 UPDATE không guard | gap | **mở trên `main`**, đóng trên wave4 (chưa merge) | `claude/wave4:tests/test_ticket_transitions.py` |
| 11-DD1 đường merge đồng bộ cũ (C9) | gap | **đóng**: không còn `def merge_candidate` trong `src/` | grep |
| 11-TĐ4 thiếu reconciliation định kỳ | gap | **một phần**: supervisor quét merge RUNNING mỗi 5 s (chỉ supervised); Planner đang chờ thì sync mỗi 60 s. Vẫn thiếu đối chiếu candidate↔main | §3.6 |
| 12-DD1 merge kẹt do barrier (C2) | gap | **đã kiểm chứng**; còn ca "ticket đổi version trong lúc merge" | `test_integration_async.py:567`; `test_operational_recovery.py:108` |
| 12-DD1 Planner job bị bỏ rơi (C10) | gap | **đã kiểm chứng** | `test_event_wait.py:316`, `:324` |
| Git trong write lock (C3) | rủi ro #3 | **đã kiểm chứng** | `test_agent_workspace_candidates.py:305`, `test_integration_release_policy.py:307` |
| Bằng chứng phân quyền qua `LIKE '%ref%'` (C7) | gap | **đã kiểm chứng** | `test_audit_evidence_match.py:25`, `:44` |
| §3.4 khả năng chịu tải | chưa đủ bằng chứng | **vẫn chưa đủ bằng chứng**: có công cụ đo (H1) nhưng chưa có số đo nào được ghi lại, và H1 không đo test slot hay thời gian barrier | §3.3 |
| `HOH_WORKER_MODE=legacy` mặc định | gap harness | mở trên `main`, đóng trên wave4 | `claude/wave4:mcp/workers.py:29` |

### 3.2 Barrier như một lock toàn cục: barrier phân vùng theo scope có an toàn không?

**Hiện trạng.** `ExecutionGate.is_closed` (`service/_common.py:407-415`) đóng toàn project
khi có *bất kỳ* reconciliation PENDING nào. Sáu nguồn mở barrier: sync thấy commit mới,
merge thành công, QA reject (`qa.py:909-918`), gate regression, base drift
(`integration.py:928`), và baseline. H8 đã tự resolve `keep` một trường hợp hẹp: merge mà
**mọi** ticket mở đều có scope root không giao với các path vừa merge, không ticket nào
phụ thuộc ticket vừa merge, và không đụng `docs/`, `spec/` hay file ở root
(`repository_reconciliation.py:404-431`, `:482-532`; test
`test_integration_async.py:745`, `:766`). Đề xuất đang xét là bước tiếp theo: chỉ đóng các
ticket có scope giao với commit, phần còn lại chạy tiếp.

**Barrier thực ra bảo vệ cái gì?** Phải tách hai bất biến:

1. *Tính đúng của `main`.* Bất biến này **không** do barrier giữ. Nó do fast-forward-only
   cộng QA chạy trên đúng candidate dựng trên đúng base: `main` chỉ đi tới một cây đã chạy
   lệnh test đóng băng. Đây là một conditional write theo version, tức `main == base`
   (PDF 217–219). Mọi xung đột ngữ nghĩa giữa các file, kể cả khi scope rời nhau, đều phải
   đi qua rebase và QA lại trên cây mới trước khi land. Nghĩa là phân vùng barrier không thể
   đưa một cây chưa test lên `main`.
2. *Tính hợp lệ của kế hoạch* (contract, acceptance criteria, scope của các ticket mở so
   với `main` mới). Barrier giữ bất biến này. Rủi ro của việc phân vùng nằm hết ở đây.

**Nhìn theo ch07 (PDF 215–219).** Barrier toàn cục giống khóa cả khách sạn để bán một phòng:
đúng nhưng không scale (PDF 216). Phân vùng tương đương khóa theo `(hotel, room_type, date)`.
Tuy vậy, sách khóa theo khóa bản ghi mà mọi truy vấn đều lọc theo. Ở HoH, "khóa" là một
*predicate trên không gian đường dẫn*: scope là tập đường dẫn được **ghi**, không phải tập
đường dẫn được **đọc** hay phụ thuộc. Ticket B ghi `src/reporting/**` nhưng gọi API trong
`src/checkout/**` mà A vừa đổi chữ ký thì không giao nhau theo scope, nhưng giao nhau về ngữ
nghĩa. Vì vậy:
- Frozen set phải được tính và kiểm **trong cùng transaction** với claim/submit/merge
  (guarded write), và phải dựa trên **hợp** của mọi reconciliation đang PENDING. Bản
  reconciliation thứ hai mở ra khi bản thứ nhất chưa đóng không được thu nhỏ tập đã đóng.
- Predicate trên path không thể chuyển thành row lock hay constraint. Nó chỉ an toàn khi
  writer còn serialize (đúng hiện nay; xem 07-DD1 mục a).
- Khi resolve, quyết định `revise` có thể **nới** scope của một ticket bị đóng sang vùng mà
  một ticket không bị đóng đang giữ lease. Phải kiểm lại luật va chạm lease ở bước resolve.

**Nhìn theo ch12 (PDF 351, 356, 371–373).** Coordination scope phải trùng với miền bất biến:
TC/C và Saga chỉ phối hợp những participant mà giao dịch thực sự chạm tới. Thao tác trong
một partition không cần coordinator, còn thao tác xuyên partition thì cần. Áp vào HoH: miền
bất biến của "kế hoạch còn hợp lệ" là *bao đóng phụ thuộc* của commit, gồm write scope giao
nhau, cạnh dependency (trực tiếp và bắc cầu), contract nhắc tới path hoặc symbol đã đổi, và
docs/spec. Một commit chạm `docs/` hay `spec/` thuộc miền toàn cục. Phân vùng chỉ an toàn khi
bao đóng đó được xấp xỉ **từ phía bảo thủ**: thiếu thì đóng toàn bộ.

**Phân loại theo nguồn reconciliation.** Đây là phần kết luận mạnh nhất:

| Nguồn | `main` có đổi? | Ticket có thể bị ảnh hưởng | Phân vùng |
| --- | --- | --- | --- |
| QA reject (`qa_failed`) | **Không** | chính ticket bị reject và các ticket phụ thuộc nó | **An toàn ngay**: không có commit mới nào trên `main` có thể làm sai kế hoạch của ticket khác. Khóa toàn project ở đây là chi phí thuần |
| Gate regression (`integration_gate_failed`, `advance_mainline=False`) | Không | như trên | An toàn ngay, cùng lý do |
| Merge của HoH (`integrated`) | Có, biết đủ path và ticket nguồn | các ticket có scope giao, ticket phụ thuộc, ticket có contract nhắc path đã đổi | An toàn ở mức thiết kế nếu bao đóng bảo thủ; rủi ro ngữ nghĩa giữa file vẫn còn nhưng QA sau rebase sẽ chặn ở `main` |
| Commit ngoài HoH (sync), base drift do commit ngoài | Có, không có contract hay ticket nguồn | không biết | Giữ **toàn cục** |

**Rủi ro còn lại khi phân vùng merge** (xung đột ngữ nghĩa): (1) lãng phí công, vì ticket
không bị đóng tiếp tục làm theo contract đã lỗi thời; (2) QA kiểm đúng một contract đã lỗi
thời: QA pass nhưng hành vi sai so với ý định mới. Rủi ro (2) chỉ bị chặn nếu suite hoặc
scenario bắt được. Cả hai không làm hỏng `main` theo nghĩa test, nhưng (2) đi ngược mục
đích của barrier.

**Khuyến nghị.**
1. Làm ngay cho `qa_failed` và `integration_gate_failed`: frozen set = ticket đó cộng bao
   đóng các ticket phụ thuộc; ticket khác chạy tiếp. Đây là một transition và một predicate
   trong `ExecutionGate.require_open(connection, project_id, ticket_id)`, đúng luật 6
   ("encode constraints in the platform").
2. Với merge, chạy **shadow mode** trước: ghi frozen set đã tính vào reconciliation nhưng
   vẫn đóng toàn cục, rồi so với quyết định thật của Planner. False negative là trường hợp
   Planner chọn `revise`, `cancel` hoặc `supersede` cho một ticket nằm ngoài frozen set.
   Chỉ bật khi tỉ lệ false negative bằng 0 trên N reconciliation có chủ đích (chủ dự án
   chọn N).
3. Thêm `read_scope` hoặc `depends_on_paths` tùy chọn cho ticket để bao đóng không chỉ dựa
   vào write scope.
4. Không phân vùng reconciliation do commit ngoài HoH.

Theo luật 7, cần đọc release note đã đưa barrier vào (0.3.26 có H8; bản gốc ghi trong
`docs/releases/README.md`) trước khi nới.

### 3.3 Một test slot trên host: hàng đợi và năng lực

**Cơ chế.** Supervisor không khởi động job `qa` hay `merge` khi đã có một job `qa` hoặc
`merge` đang chạy (`supervisor.py:220-224`; test `test_supervisor.py:6`
`test_serializes_memory_heavy_jobs_but_keeps_git_worker_capacity`). Desktop chạy
`max_workers=2` (`deploy/desktop/hoh_desktop.py:37`), worker timeout 2.100 s, test timeout
của profile tối đa 1.800 s (`service/project_policy.py:39`). Lý do của slot là bộ nhớ:
release 0.3.30 ghi rằng nhiều worker hơn làm pagefile phình tới đầy ổ hệ thống. Ở legacy
mode (mặc định trên `main`) **không có slot nào**: QA và gate có thể chạy đồng thời, tức
đúng rủi ro bộ nhớ mà slot được tạo ra để chặn.

**Ước lượng theo phương pháp của sách.** Sách tính năng lực bằng
*tải ÷ năng lực một node*, và nhân tải theo số thao tác thật mỗi giao dịch: một lần chuyển
tiền là hai thao tác trên tài khoản (PDF 343). Với funnel thì đi ngược từ TPS cuối (PDF
200–201). Áp vào HoH:

- Năng lực slot: `C = 3600 / S` lượt chạy suite mỗi giờ, với `S` là thời gian một lượt.
  Mốc từ release note (chưa phải phép đo của lệnh test đã cấu hình cho project): suite của
  HoH khoảng 5 phút với `-n 6`, 17,6 phút khi chạy tuần tự. Vậy `C ≈ 12/h` hoặc `≈ 3,4/h`.
- Số lượt mỗi ticket đã land: `r = 1 (QA) + g (gate: 0 khi dùng lại kết quả QA, 1 khi phải
  chạy) + q (số vòng QA reject) + d (số vòng drift)`. Đây là hệ số khuếch đại tương tự "2
  thao tác mỗi lần chuyển".
- Tải: `L` ticket/ngày ⇒ `L·r·S` phút slot mỗi ngày. Ví dụ minh họa (giả định, không phải
  số đo): `L = 20`, `r = 1,5`, `S = 5` ⇒ 150 phút mỗi ngày. Tính theo trung bình, slot chưa
  phải nút thắt.
- Nhưng **trung bình che mất burst**. Sách tách average và peak bằng một giả định riêng.
  HoH tự tạo burst ở hai chỗ: (1) khi barrier được resolve, mọi wake-up được re-arm cùng
  lúc, nên claim, submit và QA dồn vào một cửa sổ; (2) FF-only biến n ticket cùng qua QA
  trên một base thành tệ nhất n(n+1)/2 lượt chạy (07-TĐ4). Ví dụ n = 4 và S = 5: 10 lượt,
  tức 50 phút slot, thay vì 20.
- **Head-of-line blocking.** Một merge có `gate_mode: reused_qa_run` chỉ cần vài giây Git,
  nhưng vẫn chiếm loại job `merge` và phải chờ sau một lượt QA đang chạy, tệ nhất tới
  2.100 s. Merge chờ càng lâu thì xác suất drift của các ticket khác càng cao.
- **Lease trong hàng đợi.** Job QA hoặc merge xếp hàng mang `lease_id`. Nếu agent không
  renew trong lúc chờ (`work.wait_for_event` có renew; `event_wait.py` gia hạn 900 s mỗi
  lượt), preflight sẽ fail với lỗi lease (`qa.py:397-410`) và lượt chạy bị bỏ.

(Ngoài sách: nếu cần mô hình hàng đợi thì dùng Little's law `L = λ·W` và xấp xỉ một server
`W_q ≈ ρ/(1−ρ)·S` với `ρ = λ·S`; khi `ρ` vượt khoảng 0,7 thì thời gian chờ tăng rất nhanh.
Đây là lý thuyết hàng đợi chuẩn, không phải nội dung của sách, và cần số đo `λ`, `S` mới có
nghĩa.)

**Phải đo, không được đoán** (mọi trường đều đã có trong schema; chỉ cần truy vấn):

| Đại lượng | Nguồn |
| --- | --- |
| `S` theo loại job, p50/p90 | `qa_test_runs.duration_ms`; `merge_jobs.completed_at - started_at` theo `gate_mode` |
| `W_q` chờ slot | `qa_test_jobs.started_at - created_at`; `merge_jobs.started_at - created_at` |
| `λ` mỗi giờ theo loại, kể cả burst sau barrier | `created_at` của hai bảng job, căn theo `repository_reconciliations.resolved_at` |
| Utilization của slot | tổng thời gian bận ÷ thời gian thực |
| `r` thực mỗi ticket, tách `q` và `d` | `qa_test_runs` theo ticket; `merge_jobs.merge_status='base_drift'`; event `integration.base_drift` |
| Tỉ lệ gate dùng lại kết quả QA | `cycle_metrics.gate_runs` |
| Thời gian barrier đóng và tỉ lệ thời gian project bị đóng | `repository_reconciliations.resolved_at - created_at`; hợp của các khoảng PENDING |
| Lượt chạy bị bỏ vì lease hết hạn khi chờ | `qa_test_jobs.status='FAILED'` có `error_message` về lease |
| Bộ nhớ đỉnh của một lượt suite so với RAM | đo RSS hoặc commit charge trên host: đây là số quyết định có thể lên 2 slot hay không |

**Hướng sửa có thể làm trước khi có số đo:** merge có gate dùng lại kết quả QA thì không
chiếm slot nặng; trong hàng đợi slot, ưu tiên merge trước QA (land sớm làm giảm drift); đưa
`W_q` và thời gian barrier vào `cycle_metrics`.

### 3.4 Idempotency sau C6

**HoH làm gì** (`service/idempotency.py`):
- `reserve` → `run()` → `complete` là ba transaction (`:92-101`). Key gắn
  `(actor, project, operation, key)` và hash payload; khác payload thì `payload_mismatch`
  (`:123-127`).
- Request trùng đang chạy trả `in_progress` (`:131-135`); key IN_PROGRESS quá 600 s thì bị
  xóa và trả `idempotency_unknown` (`:136-168`). Lần retry sau đó chạy lại lệnh, còn các
  guard domain (version, lease, snapshot) chặn effect trùng.
- `complete` và `release` tự retry contention, không bao giờ biến một effect đã commit thành
  lỗi (`:171-214`).
- MCP còn một lớp retry thô: chạy lại *toàn bộ* `run_idempotent` khi gặp `ContentionError`,
  dựa trên giả định "ContentionError chỉ đến từ một transaction chưa bắt đầu"
  (`mcp/invoke.py:21-39`).

**Đối chiếu ch11.** PDF 332 định nghĩa exactly-once = at-least-once (retry) cộng at-most-once
(idempotency). PDF 334–335: key do client sinh; lần lặp lại trả *trạng thái mới nhất* của
request trước; nhiều request đồng thời cùng key thì chỉ một được xử lý, số còn lại nhận
429. Hiện thực bằng **unique constraint của DB**: insert thành công nghĩa là chưa thấy
request này. PDF 336: với hệ thống ngoài, dùng cùng key và vẫn phải reconcile.
- `in_progress` khớp với 429. Hash payload khớp tinh thần "cùng request".
- **Chỗ lệch:** sách để dedupe và effect trên *cùng* bản ghi (PDF 212: `reservation_id` là
  primary key của bảng reservation; PDF 335: primary key làm idempotency key). HoH để chúng
  ở hai bảng và ba transaction, nên mới có trạng thái "unknown". Với PSP, unknown là không
  tránh được vì effect nằm bên ngoài. **Với HoH, effect của hầu hết lệnh nằm ngay trong DB
  của HoH**, nên unknown là tránh được: mọi state change ghi một `audit_events` cùng
  transaction, mang `correlation_id` của request (`validation.py:140-185`;
  `invoke.py:88` mở `correlation_scope`). Nếu reservation lưu `correlation_id`, thì khi
  reclaim chỉ cần
  `SELECT 1 FROM audit_events WHERE correlation_id = ?` là biết chắc effect đã commit hay
  chưa, và trả "trạng thái mới nhất" như PDF 334 thay vì `idempotency_unknown`.

**Đối chiếu ch07.** PDF 210–212 tách hai bài toán: cùng request lặp lại (idempotency key) và
nhiều request tranh nhau (lock, version, constraint). Sau C6, HoH chủ động dùng lời giải
của bài toán thứ hai để chặn trùng của bài toán thứ nhất sau khi key bị reclaim. Cách này
fail-closed, nhưng caller nhận *conflict* chứ không nhận *kết quả cũ*, nên phải đọc lại và tự
suy ra. Cách này đúng chừng nào **mọi** tool có key cũng có guard tự nhiên. Kiểm 19 tool
Planner/common có key và các tool claim/submit/verdict/merge: create dùng ID do client
cấp (khóa tự nhiên), transition dùng `expected_version`, report failure dùng fingerprint.
Không thấy ngoại lệ, nhưng cũng không có test nào liệt kê đủ.

**Lỗ hổng còn lại cùng loại với C6 (mới phát hiện).** `claim_ticket` commit claim, sau đó mở
**transaction ghi thứ hai** để ghi context snapshot
(`leases.py:345-350` gọi `get_ticket_context`, ghi tại `context.py:698-708`). Nếu transaction
thứ hai gặp `ContentionError` sau khi store đã hết lượt retry nội bộ, thì `run()` raise,
`_release_idempotency_record` xóa key, và lớp retry của MCP chạy lại claim. Lần này claim bị
từ chối vì ticket đã ở DEV_IN_PROGRESS với version mới. Kết quả: agent nhận lỗi trong khi
thực tế đang giữ lease, và ticket kẹt tới khi lease 900 s hết hạn. Giả định trong
`invoke.py:21-24` sai với mọi lệnh có hơn một transaction ghi. wave5 còn thêm `worker_brief`
sau claim (`claude/wave5:service/leases.py`), tuy chỉ đọc.

**Lỗi nhỏ:** complete và release theo scope chứ không theo `record_id` (ABA, xem 06-DD1c).
wave5 nhận SHA rút gọn: cùng key mà lần retry gửi SHA đầy đủ thay cho SHA rút gọn sẽ bị
`payload_mismatch` (fail-closed, chấp nhận được).

**Kết luận.** 11-DD1 cho lệnh nội bộ: **đáp ứng ở mức thiết kế, một phần đã kiểm chứng**
(`test_idempotency.py:174`
`test_a_duplicate_start_while_the_first_is_in_flight_is_rejected`, `:358`, `:410`,
`:87`). Ranh giới nguyên tử theo tiêu chí ch06 (PDF 186) và ch11 (PDF 335): **chưa đáp
ứng**. Lệnh nhiều transaction: **chưa đáp ứng** (không có test; lập luận từ code). Phía Git
(merge): **đã kiểm chứng** (§2.4).

### 3.5 Audit events và reproducibility

**Hiện trạng.** ARCHITECTURE nói rõ: event là audit trail, không thể dựng lại state từ
event. Payload mỏng (ví dụ claim chỉ có `{"lease_id"}`). Có `correlation_id` và
`causation_id` để lần chuỗi (`observability.py` `trace`). Tuy vậy event đã thành **input cho
quyết định** ở ít nhất ba chỗ:
- bằng chứng gate cho recovery: `_queued_gate_is_recorded` đọc event
  `integration.gate_passed` (`integration.py:764-772`);
- `cycle_metrics` dựng các phase từ event (`board.py:330-356`);
- **wave5**: rework budget *đếm* event `ticket.rework_requested`,
  `ticket.rework_budget_exhausted` và `ticket.rework_budget_reset`
  (`claude/wave5:service/qa.py:694-706`).

**Tiêu chí sách.** PDF 343: reconciliation chỉ cho thấy *có* chênh lệch, không cho biết chênh
lệch sinh ra *như thế nào*; reproducibility là dựng lại lịch sử bằng replay từ đầu. PDF
356–361: command là ý định, có thể chứa I/O và ngẫu nhiên; event là sự thật đã kiểm, phải
tất định; state machine phải tất định; replay trả lời ba câu của auditor (state tại thời
điểm bất kỳ, state có đúng không, logic có còn đúng sau khi đổi code). PDF 367: state và
snapshot luôn tái tạo được từ event list, còn command thì không.

**HoH có cần reproducibility không?** Đề xuất: **không cần event sourcing đầy đủ**. Kết luận
**không áp dụng** cho dạng reproducibility của ch12, *nếu* chủ dự án đồng ý. Lý do:
- Command của HoH vốn không tất định: chúng gọi Git, chạy suite, nhận output của LLM. Theo
  PDF 357/367, chỉ event mới replay được, nên muốn replay thì phải lưu toàn bộ *kết quả*
  của I/O vào event (SHA, digest của evidence, verdict). Phần lớn những thứ này đã nằm ở các
  bảng `candidates`, `qa_test_runs` và `evidence`.
- HoH không chuyển tiền. Không có auditor bên ngoài hỏi "state lúc T là gì".

**Điều HoH cần** (và hiện **chưa đủ bằng chứng**): *giải thích được và kiểm được* trạng thái
ticket từ log. Cụ thể:
1. Mọi transition ticket ghi một event có `from_status`, `to_status` và `version`, trong
   **cùng** transaction. wave4 đã tạo đúng một choke point (`_transition_ticket`), nên việc
   này chỉ cần thêm vài dòng ở đó.
2. Một **fold test**: gập các event transition của mọi ticket phải ra đúng `(status,
   version)` hiện tại. Đây là reconciliation giữa view và log. Nó yếu hơn replay nhưng trả
   lời được câu "chênh lệch sinh ra thế nào" mà PDF 343 nói reconciliation thuần không trả
   lời được.
3. Event nào được dùng làm input (gate proof, rework counter, metrics) phải có hợp đồng
   schema: tên event, các trường bắt buộc, test khóa lại. Nếu không, đổi tên một event sẽ âm
   thầm làm budget đếm sai. Cách thay thế: rework counter thành một cột state, cập nhật trong
   cùng transition.

Quyết định "có cần reproducibility hay không" vẫn là của chủ dự án. Review trước đã nêu câu
hỏi này và đến nay chưa có câu trả lời ghi lại.

### 3.6 Git như hệ thống ngoài: reconciliation đã đủ chưa?

**Đã có** (đúng PDF 326, 334–336):
- Intent ghi trước effect, recover bằng cách hỏi Git, bằng chứng gate gắn với job (§2.4,
  **đã kiểm chứng**).
- Quét định kỳ: supervisor reconcile mọi merge RUNNING mỗi 5 s
  (`supervisor.py:117-140`, `:206-208`), **chỉ ở supervised mode**.
- Quan sát `main` và remote: `sync_repository` chạy mỗi 60 s, **chỉ khi có Planner đang
  chờ** (`event_wait.py:129-150`). CAS chống lost update (C8, **đã kiểm chứng**).
- Landing ngoài gate được ghi là ungated, và server tự kiểm commit có thật trên branch hay
  không (`integration.py:90-160`).

**Còn thiếu** (đối chiếu PDF 328–329: loại 1, tự động phân loại và sửa được, phải được tự
động hóa):

1. **Candidate đã nằm trên `main` bị phân loại nhầm thành drift.** Khi người vận hành
   fast-forward tay một candidate đã qua QA, thì `queue_merge_candidate` từ chối với
   `base_drift` (`integration.py:463-490`). Còn nếu merge tay xảy ra *sau* khi queue,
   `_execute_queued_merge_locked` thấy `current_main != base` và ghi **REWORK** kèm một
   reconciliation drift (`integration.py:673-674` → `:894-968`). Cả hai đường đều không hỏi
   `commit_is_on_branch(candidate)`. Hậu quả: Developer bị yêu cầu rebase một việc đã land,
   và sự thật ("đã land ngoài gate") bị ghi thành "drift". Đây là mismatch loại 1: phân loại
   được bằng một lệnh `merge-base --is-ancestor`. **Chưa đáp ứng.**
2. **Không ghép commit quan sát được với candidate của ticket đang mở.** Sync liệt kê commit
   nhưng không đánh dấu "commit này là `candidate_commit` của ticket X đang MERGE_READY".
   Planner phải tự nhận ra rồi gọi `record_external_landing`. **Chưa đáp ứng** (loại 1).
3. **Merge đã land nhưng ticket đổi version trong lúc chạy** → job ở RUNNING mãi
   (`integration.py:882`, có chủ đích theo `test_operational_recovery.py:108`). Lúc đó
   `record_external_landing` từ chối vì còn job RUNNING (`:130-140`), barrier do sync mở ra
   không resolve được bằng `complete`, và chỉ admin gỡ được. Đây là loại 2 (phân loại được,
   sửa tay), nhưng hàng đợi chỉ đọc theo kiểu pull. **Đáp ứng ở mức thiết kế cho phát hiện;
   chưa đáp ứng cho phát tới người.**
4. **Quan sát Git phụ thuộc vào việc Planner có mặt.** Không có Planner nào chờ thì không ai
   phát hiện commit ngoài. `main` vẫn an toàn nhờ FF-only (merge kế tiếp sẽ thấy drift),
   nhưng các mismatch loại 1 ở trên càng không được thấy.
5. **Chiều ngược lại:** không có kiểm tra định kỳ rằng commit của ticket DONE vẫn reachable
   từ `main` (sau history rewrite). Sync có ghi `history_relation`, nên Planner thấy nếu có
   mặt. Ưu tiên thấp.
6. FF dạng check-then-act (§2.4). Ưu tiên thấp.

**Kết luận 11-TĐ4:** **đáp ứng một phần.** Đối chiếu theo *sự kiện* (recover một job cụ thể)
**đã kiểm chứng**. Đối chiếu theo *trạng thái* (quét định kỳ toàn bộ ticket mở so với Git)
**chưa đáp ứng**. Sách đòi cả hai, kể cả khi đã có idempotency (PDF 336).

---

## 4. Danh sách gap ưu tiên

Thứ tự: correctness trước, chi phí vòng lặp sau, cấu trúc cuối. Mỗi mục là một ticket HoH
tiềm năng. Mục nào làm yếu một bất biến phải đọc release note gốc trước (luật 7).

### P0 — correctness

| # | Gap | Sửa | Test kiểm chứng |
| --- | --- | --- | --- |
| G1 | Candidate đã trên `main` bị coi là drift → REWORK sai (§3.6.1) | Trước khi phân loại drift (`integration.py:673`, `:463-490`), hỏi `commit_is_on_branch(candidate_commit)`. Nếu đúng: không REWORK; job kết thúc với reason `candidate_already_on_main`; mở reconciliation mang gợi ý "ghi landing ungated" (không ghi QA approval, theo luật 5) | Queue merge → fast-forward tay `main` tới candidate → chạy worker: ticket **không** ở REWORK, job FAILED với reason đúng, reconciliation chỉ ra ticket; biến thể "merge tay trước khi queue" cũng vậy |
| G2 | Lệnh nhiều transaction biến effect đã commit thành lỗi (claim → snapshot; §3.4) | (a) Phần làm giàu sau commit chạy best-effort: bắt `ContentionError`, trả kết quả claim không kèm `current_context_snapshot_id`; (b) sửa giả định ở `invoke.py:21-24`: chỉ retry khi chưa transaction ghi nào commit (đánh dấu qua contextvar) | Monkeypatch `get_ticket_context` raise `ContentionError` sau khi claim commit: MCP `work.claim` trả lease, `leases` có đúng 1 dòng, không có `ConflictError`, key idempotency COMPLETED |
| G3 | Merge đã land, ticket đổi version → RUNNING mãi, landing bị từ chối (§3.6.3) | Ở nhánh `recovered and merged`: nếu `candidate_commit` khớp intent và gate đã ghi thì vẫn complete (Git là sự thật, cùng lập luận với C2) và mở reconciliation; hoặc chuyển job sang trạng thái kết thúc `LANDED_NEEDS_REVIEW` để `record_external_landing` chạy được. Đọc RELEASE_0.3.26 (C2) trước | Sửa `test_recovery_retains_changed_ticket_for_operator_review` để kỳ vọng job kết thúc, ticket ở trạng thái landed hoặc reconciliation mở, và không còn job RUNNING |
| G4 | `idempotency_unknown` tránh được; ABA khi complete/release | Lưu `correlation_id` vào `idempotency_records` lúc reserve; khi reclaim, có event mang correlation đó thì trả `{outcome: committed, ...trạng thái mới nhất}` (PDF 334), không có thì release và chạy. Complete/release theo `record_id` | (1) crash sau `run()` → reclaim trả committed, không chạy lại; (2) crash trước khi effect commit → retry chạy lệnh; (3) request gốc hoàn tất muộn không ghi đè reservation mới |

### P1 — chi phí vòng lặp và vận hành

| # | Gap | Sửa | Test kiểm chứng |
| --- | --- | --- | --- |
| G5 | QA reject và gate regression đóng cả project dù `main` không đổi (§3.2) | Frozen set = ticket đó cộng bao đóng phụ thuộc; `ExecutionGate.require_open(connection, project_id, ticket_id)` và offer filter theo frozen set; frozen set lưu trên dòng reconciliation | QA reject T1: claim, submit, merge của T2 (scope rời, không phụ thuộc) chạy được; T1 và ticket phụ thuộc T1 vẫn bị chặn; sync mở một reconciliation toàn cục thì chặn tất cả |
| G6 | Phân vùng cho merge chưa có bằng chứng an toàn (§3.2) | Shadow mode: tính và ghi `would_freeze` cho mỗi reconciliation của merge; báo tỉ lệ false negative so với quyết định Planner; thêm `read_scope` tùy chọn | Unit test tính frozen set (giao scope, dependency bắc cầu, docs/root → toàn cục, hợp của các PENDING); test metric false negative trên fixture |
| G7 | Merge dùng lại kết quả QA vẫn chiếm slot nặng và đứng sau QA (§3.3) | Ở `supervisor.py:223`, chỉ tính là slot nặng khi gate phải chạy suite; ưu tiên `merge` trước `qa` trong `_pending` | Tick với một QA đang chạy và một merge queued có QA run dùng lại được: merge được khởi động |
| G8 | Không đo được năng lực (§3.3) | Thêm vào `cycle_metrics`/`project_metrics`: `W_q` và `S` theo loại job (p50/p90), utilization, `r` gồm `q` và `d`, thời gian barrier và tỉ lệ thời gian bị đóng, số lượt chạy bị bỏ vì lease; tính cả ticket đang dở | Fixture với timestamp biết trước → số liệu kỳ vọng chính xác |
| G9 | Vòng drift không có budget (§2.4) | Đếm base drift theo ticket; vượt N thì escalate cho Planner (cùng cơ chế rework budget của wave5, đếm bằng cột state chứ không đếm event) | Drift lần N+1 → ticket BLOCKED kèm reason riêng |
| G10 | Leo thang barrier và `needs_attention` không tới được người (§2.6) | Bảng notification bền với trạng thái `pending → sent → acked`; kênh cấu hình được (toast desktop, webhook); retry tới khi ack; phát khi không có waiter hoặc khi vượt mức 2 | Không có waiter, barrier quá 2 h → đúng một notification được ghi, gửi qua kênh giả, retry khi kênh lỗi |
| G11 | Phát hiện Git phụ thuộc Planner (§3.6.4) | Supervisor quét định kỳ ở chế độ *chỉ phát hiện*: fetch đọc-chỉ, so `candidate_commit` của các ticket MERGE_READY/VERIFIED với `main`, kiểm commit của ticket DONE còn reachable; mismatch vào `needs_attention` và G10. Việc mở barrier vẫn để Planner | Candidate được merge tay khi không có Planner → xuất hiện một mục `needs_attention` mang loại mismatch |

### P2 — cấu trúc và độ bền

| # | Gap | Sửa | Test kiểm chứng |
| --- | --- | --- | --- |
| G12 | wave4 chưa merge; wave5 xung đột với nó ở `qa.py` | Merge wave4 trước; chuyển `UPDATE tickets SET blocked_reason` của wave5 (`qa.py:925`) vào `_transition_ticket` | Nhánh gộp pass `test_ticket_transitions.py` và `test_rework_budget.py`; `test_no_ticket_update_bypasses_the_guarded_transition` xanh |
| G13 | Event dùng làm input nhưng không có hợp đồng; không kiểm được trạng thái từ log (§3.5) | Event transition (from, to, version) ghi trong `_transition_ticket`; hợp đồng cho các event được đọc; rework counter thành cột | Fold test: với mọi ticket, gập các transition ra đúng `(status, version)` hiện tại |
| G14 | FF dạng check-then-act với writer ngoài lock (§2.4) | `git update-ref <ref> <new> <old>` (CAS), rồi đồng bộ working tree; hoặc ghi rõ trong ARCHITECTURE rằng checkout dùng chung chỉ được ghi dưới lock | Chèn một commit ngoài giữa check và merge → merge từ chối, ref không bị ghi đè |
| G15 | Độ bền một host: `synchronous=NORMAL`, chưa diễn tập backup/restore | Diễn tập backup/restore DB + Git + evidence, đo RPO/RTO (RUNTIME_OPERATIONS.md:148); cân nhắc `synchronous=FULL` cho các transaction quan trọng | Restore từ backup lên host sạch → supervisor recover → các merge RUNNING hội tụ đúng theo Git |
| G16 | Lint "mọi write lấy lock" chỉ quét `service/` và `event_wait.py` | Mở rộng ra mọi module trong `src/hoh_control_plane` | Chính test đó, với danh sách ngoại lệ trống |

---

**Những gì chưa kiểm:** không chạy test nào (theo yêu cầu); không đọc DB live nên mọi nhận
định về tần suất (drift, barrier, unknown) là suy luận từ cơ chế; chưa kiểm hết 19 tool có
key để chắc mỗi tool có guard tự nhiên (chỉ xem chữ ký và các tool chính); chưa đọc PDF
348–349 và 362–374 ngoài phần dò từ khóa.
