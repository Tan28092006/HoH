# Multi-Agent Engineering Control Plane

## 1. Mục tiêu

Tài liệu này mô tả một hạ tầng phát triển phần mềm trong đó nhiều AI agent độc lập cùng làm việc trên một trạng thái dự án chung thông qua MCP. Mỗi agent có vai trò, quyền hạn, local skill và tiêu chuẩn hoàn thành riêng. Con người và agent cùng quan sát tiến độ qua một giao diện Kanban và các màn hình evidence, agent activity, feature map và audit.

Hệ thống lấy cảm hứng từ bài báo [Harness-of-Harness: Multi-Day Autonomous Software Development with Continual Improvement](./2609.01481v1.pdf), nhưng chuyển mô hình từ một vòng lặp tuần tự do một orchestrator điều khiển thành một control plane dùng chung cho nhiều agent độc lập.

Mục tiêu chính:

1. Quản lý nhiều dự án theo cấu trúc `Project -> Feature -> Subfeature -> Ticket`.
2. Cho phép agent khám phá công việc phù hợp với vai trò của mình qua Metadata MCP.
3. Cấp đúng context cho đúng agent, đúng ticket và đúng phiên bản artifact.
4. Duy trì ranh giới quyền hạn giữa Planner, Developer, QA và Integrator.
5. Cho phép nhiều agent làm việc song song mà không ghi đè hoặc kiểm thử nhầm phiên bản.
6. Biến test result, log, screenshot, trace và QA verdict thành evidence có provenance.
7. Hiển thị toàn bộ trạng thái qua Web UI phục vụ cả con người và agent.
8. Cho phép tích hợp Jira, GitHub Issues hoặc hệ thống khác mà không phụ thuộc chúng làm lõi.

## 2. Nguyên tắc thiết kế

### 2.1. Control plane là nguồn sự thật về quy trình

Kanban, Jira và MCP đều là các giao diện truy cập. Trạng thái workflow, lease, dependency, permission và audit phải được quản lý trong control plane.

### 2.2. Git là nguồn sự thật về artifact

Mọi candidate phải được nhận diện bằng giá trị bất biến, ưu tiên Git commit SHA. Không dùng mô tả như “bản mới nhất” làm identity cho một lần QA.

### 2.3. Evidence phải gắn với candidate

Mỗi test result, log, screenshot hoặc QA finding phải chỉ rõ:

- Candidate commit hoặc artifact digest.
- Test environment.
- Lệnh hoặc kịch bản tái tạo.
- Người hoặc agent tạo evidence.
- Thời điểm tạo.
- Claim hoặc acceptance criterion mà evidence hỗ trợ hay phủ định.

### 2.4. Context là dữ liệu có cấu trúc, không phải chat history

Không truyền toàn bộ lịch sử hội thoại cho agent. Context được biên soạn theo vai trò, nhiệm vụ, dependency và token budget từ các bản ghi có provenance.

### 2.5. Một writer trên một scope tại một thời điểm

Một ticket phát triển chỉ có một lease ghi hợp lệ. Hai ticket có file scope giao nhau không được chạy song song trừ khi Planner hoặc Integrator cho phép rõ ràng.

### 2.6. QA độc lập với Developer

QA đọc candidate bất biến và không được sửa production code. Nếu QA phát hiện lỗi, hệ thống tạo finding hoặc rework ticket thay vì để QA sửa trực tiếp.

### 2.7. Agent không tự cấp thêm quyền

Local skill hướng dẫn cách làm việc nhưng backend mới là nơi cưỡng chế quyền. Prompt hoặc skill không được xem là security boundary.

### 2.8. Mọi thay đổi quan trọng đều có audit event

Không chỉ lưu trạng thái cuối. Hệ thống phải lưu ai đã claim, gia hạn lease, đổi acceptance criteria, submit candidate, chạy test, pass/fail hoặc reopen ticket.

## 3. Mô hình khái niệm

```text
Organization
└── Project
    ├── Product specification / PRD
    ├── Feature
    │   ├── Subfeature
    │   │   ├── Ticket
    │   │   │   ├── Context entries
    │   │   │   ├── Decisions
    │   │   │   ├── Candidates
    │   │   │   ├── Evidence
    │   │   │   └── QA findings
    │   │   └── Ticket
    │   └── Feature-level context rollup
    ├── Agent registry
    ├── Skill registry
    └── Audit event stream
```

### 3.1. Project

Project đại diện cho một repository hoặc một nhóm repository cùng phục vụ một sản phẩm.

Thuộc tính quan trọng:

- `project_id`
- `name`
- `description`
- `repository_refs`
- `default_branch`
- `product_spec_ref`
- `status`
- `policy_profile_id`
- `created_at`, `updated_at`

### 3.2. Feature và Subfeature

Feature là cây đệ quy. Một feature có thể chứa subfeature hoặc ticket lá.

Thuộc tính:

- `feature_id`
- `project_id`
- `parent_feature_id`, có thể rỗng
- `title`
- `objective`
- `status`
- `priority`
- `owner_role`
- `context_ref`
- `progress_summary`
- `version`

Feature context là bản tổng hợp, không thay thế ticket context. Nó trả lời:

- Feature nhằm đạt kết quả sản phẩm nào?
- Những phần nào đã hoàn thành và được xác minh?
- Ticket nào đang mở, bị kẹt hoặc chờ QA?
- Dependency quan trọng nào chưa thỏa?
- Hành vi nào phải được bảo toàn?

### 3.3. Ticket

Ticket là đơn vị công việc có thể claim và có điều kiện hoàn thành quan sát được.

Các loại ticket ban đầu:

- `feature_work`: triển khai hành vi mới.
- `bug`: sửa lỗi đã quan sát.
- `qa_validation`: kiểm thử một candidate.
- `investigation`: thu thập thông tin trước khi lập kế hoạch hoặc sửa lỗi.
- `integration`: merge, resolve conflict hoặc chạy regression trên mainline.
- `infrastructure`: môi trường build/test/deploy.

Thuộc tính:

- `ticket_id`
- `feature_id`
- `parent_ticket_id`, dùng khi cần chia nhỏ
- `type`
- `title`
- `objective`
- `status`
- `priority`
- `required_role`
- `acceptance_criteria[]`
- `preservation_constraints[]`
- `file_scope[]`
- `dependency_ids[]`
- `blocked_reason`
- `base_commit`
- `candidate_commit`
- `context_ref`
- `version`
- `created_by`, `created_at`, `updated_at`

### 3.4. Context entry

Context entry là một mẩu kiến thức có nguồn gốc rõ ràng được gắn vào project, feature hoặc ticket.

Loại context:

- `requirement`
- `design_decision`
- `implementation_note`
- `progress_update`
- `known_failure`
- `verified_behavior`
- `preservation_constraint`
- `environment_note`
- `handoff`
- `risk`
- `question`

Mỗi entry cần có:

- Nội dung ngắn, có cấu trúc.
- `scope_type` và `scope_id`.
- Nguồn tạo: user, agent, Git commit, test run hoặc import.
- Role của người tạo.
- Mức độ tin cậy.
- Phiên bản artifact liên quan nếu có.
- Thời gian hiệu lực hoặc trạng thái superseded.

Không xóa âm thầm context cũ. Khi một quyết định thay đổi, entry mới phải tham chiếu entry bị thay thế.

### 3.5. Candidate

Candidate là artifact mà Developer gửi sang QA.

- `candidate_id`
- `ticket_id`
- `base_commit`
- `commit_sha` hoặc `artifact_digest`
- `branch_or_worktree_ref`
- `build_status`
- `developer_claims[]`
- `developer_test_refs[]`
- `submitted_by`
- `submitted_at`
- `frozen_at`

Sau khi submit, candidate là bất biến. Mọi sửa đổi tạo candidate mới.

### 3.6. Evidence và QA finding

Evidence là dữ liệu quan sát; finding là kết luận của QA dựa trên evidence.

Evidence:

- `evidence_id`
- `candidate_id`
- `criterion_id`
- `type`: test result, log, trace, screenshot, video, manual observation
- `storage_ref`
- `reproduction_command` hoặc `scenario_ref`
- `environment_digest`
- `observation`
- `created_by`
- `created_at`
- `checksum`

QA finding:

- `finding_id`
- `candidate_id`
- `criterion_id`
- `status`: verified, failed, insufficient_evidence, flaky
- `severity`
- `player_or_user_impact`
- `evidence_refs[]`
- `recommended_action`
- `created_by`

### 3.7. Lease

Lease ngăn nhiều agent cùng ghi vào một ticket.

- `lease_id`
- `ticket_id`
- `agent_id`
- `role`
- `scope`
- `acquired_at`
- `expires_at`
- `heartbeat_at`
- `lease_version`

Lease hết hạn không đồng nghĩa công việc bị xóa. Ticket chuyển về trạng thái có thể recover và lưu lại worktree/branch cùng progress cuối.

## 4. Kiến trúc tổng thể

```text
                         Web UI
             Kanban / Features / Agents / Evidence
                            |
                       UI API + Events
                            |
               Multi-Agent Control Plane
  +----------------+--------+----------+----------------+
  |                |                   |                |
  | Metadata MCP   | Role MCP Gateway  | Context Engine | Policy Engine
  |                |                   |                |
  +----------------+--------+----------+----------------+
                            |
        +-------------------+-------------------+
        |                   |                   |
   PostgreSQL          Object Storage      Git Providers
 task/state/audit      evidence/media      repo/worktree/PR
        |
   Event Dispatcher
        |
 Planner agents / Dev agents / QA agents / Integrator agents
```

### 4.1. Các thành phần

#### Control Plane API

Chứa domain logic và state machine. MCP và Web UI đều gọi chung lớp application service này để tránh hai bộ quy tắc khác nhau.

#### Metadata MCP

Điểm vào đầu tiên của agent. Nó cho agent biết:

- Dự án nào agent được truy cập.
- Vai trò và capability hiện tại.
- Feature nào đang active.
- Ticket nào phù hợp với vai trò.
- Ticket hoặc feature nào đang bị kẹt.
- `context_ref` nào cần resolve tiếp.
- MCP tool nào được phép gọi ở bước kế tiếp.

Metadata MCP không trả về command shell, URL tùy ý hoặc prompt bắt agent tự mở endpoint. Nó chỉ trả typed reference và tên tool nằm trong allowlist của server.

#### Role MCP Gateway

Có thể triển khai thành một MCP server vật lý với nhiều capability profile:

- Planner profile
- Developer profile
- QA profile
- Integrator profile

Một backend chung giúp các role nhìn thấy cùng state và giảm schema drift. Nếu cần tách process để cô lập mạnh hơn, các server vẫn phải dùng chung domain service và policy engine.

#### Context Engine

Biên soạn context packet từ project, feature, ticket, Git, evidence và audit state. Nó chịu trách nhiệm:

- Truy xuất theo scope.
- Loại entry đã superseded.
- Xếp hạng theo vai trò và nhiệm vụ.
- Giới hạn token.
- Trả provenance.
- Tạo snapshot ID để audit agent đã nhìn thấy context nào.

#### Policy Engine

Kiểm tra authorization và invariant trước mọi thao tác ghi.

#### Event Dispatcher

Phát sự kiện cho UI và cho agent scheduler. MVP có thể dùng transactional outbox trong PostgreSQL; chưa cần message broker riêng.

#### Dispatch queue và agent presence

Không xem một chat đang mở là worker chắc chắn đang chạy. Mỗi Developer/QA khi
bắt đầu hoặc resume phải đăng ký presence ngắn hạn (`agent.register`) và đọc
`work.inbox`. Control plane lưu ba lớp bền vững trong cùng transaction với
state transition:

- `work_queue`: một hand-off `pending` theo ticket, role, priority và context version;
- `dispatch_outbox`: ranh giới delivery cho scheduler, webhook hoặc adapter của từng nền tảng;
- `work_offers`: offer ngắn hạn tới các agent còn online.

Khi Dev submit candidate, ticket vào `QA_READY` và một queue item cho QA được
tạo. Khi QA fail, ticket vào `REWORK` và một queue item cho Dev được tạo. Nếu
không có agent đúng role, queue không biến mất. Khi nhiều QA/Dev check in trong
cùng khoảng thời gian, mỗi agent có thể nhận offer; chỉ `claim` thành công đầu
tiên tạo lease, đồng thời đóng những offer còn lại. Vì vậy offer không phải là
ownership và không làm mất invariant một writer cho một phase.

Stdio MCP không thể tự đánh thức một Codex/Claude chat đã dừng. Outbox cho phép
gắn adapter riêng (Codex automation, Claude hook, desktop notification, Slack
hoặc webhook) mà không để adapter thay đổi ticket state. Cho đến khi có adapter,
agent phải poll `work.inbox` lúc khởi động/resume và scheduler retry offer.

#### Object Storage

Lưu evidence lớn như log, screenshot, video, trace và report. Database chỉ lưu metadata, checksum và storage reference.

## 5. Cơ chế khám phá bằng Metadata MCP

### 5.1. Luồng chuẩn của agent

```text
1. Agent khởi động và đọc local skill của role.
2. Agent gọi `identity.get_capabilities`.
3. Execution agent gọi `agent.register`, sau đó `work.inbox`.
4. Metadata MCP trả feature/ticket phù hợp và `context_ref`.
5. Agent chọn một ticket có thể hành động.
6. Agent gọi `feature.get_context` hoặc `ticket.get_context`.
7. Agent claim ticket nếu cần quyền ghi; lease là quyết định cuối cùng khi có race.
8. Agent thực hiện vòng lặp theo role.
9. Agent ghi progress/evidence/candidate/verdict.
10. Transition tạo queue/outbox; scheduler hoặc agent check-in làm offer được retry.
```

### 5.2. Ví dụ kết quả discovery

```json
{
  "agent": {
    "agent_id": "dev-agent-03",
    "role": "developer",
    "capabilities": [
      "ticket:read",
      "ticket:claim:developer",
      "progress:append",
      "candidate:submit"
    ]
  },
  "project": {
    "project_id": "checkout-platform",
    "default_branch": "main",
    "head_commit": "7f42c1a"
  },
  "features": [
    {
      "feature_id": "payment-retry",
      "title": "Payment retry",
      "status": "in_progress",
      "context_ref": "ctx:feature:payment-retry:v18",
      "pending_ticket_refs": [
        "ticket:DEV-142",
        "ticket:BUG-153"
      ],
      "blocked_ticket_count": 1,
      "recommended_calls": [
        {
          "tool": "feature.get_context",
          "arguments": {
            "context_ref": "ctx:feature:payment-retry:v18"
          }
        },
        {
          "tool": "ticket.get_context",
          "arguments": {
            "ticket_ref": "ticket:DEV-142"
          }
        }
      ]
    }
  ],
  "next_cursor": null
}
```

`recommended_calls` chỉ là lời gọi typed tới tool đã đăng ký. Server phải kiểm tra lại quyền khi lời gọi thực sự diễn ra; việc xuất hiện trong metadata không tự cấp quyền.

### 5.3. Bộ lọc discovery

`work.discover` nên hỗ trợ:

- `project_id`
- `role`
- `status[]`
- `blocked_only`
- `awaiting_role`
- `feature_id`
- `priority_min`
- `dependency_ready`
- `unassigned_only`
- `cursor`
- `limit`

Danh sách lớn dùng cursor pagination để ổn định khi ticket mới liên tục được tạo.

## 6. MCP tool contract đề xuất

Tên dưới đây là logical contract. Khi hiện thực có thể gom chúng vào namespace phù hợp với SDK được chọn.

### 6.1. Công cụ chung

#### `identity.get_capabilities`

Trả identity, role, project scope, permissions và skill version bắt buộc.

#### `agent.register` và `agent.heartbeat`

Đăng ký hoặc gia hạn presence ngắn hạn cho authenticated agent. Chỉ role/project
scope lấy từ identity đã xác thực được lưu; `skills`, `capacity` và `machine_id`
chỉ là metadata dispatch, không cấp quyền.

#### `work.inbox`

Trả các offer bền vững phù hợp role/project. Khi không có offer, agent mới dùng
`work.discover` để xem metadata rộng hơn. Agent phải đọc ticket context và vẫn
phải `claim` với `expected_version`; inbox offer không phải lease.

#### `workspace.list_projects`

Liệt kê các project agent được đọc.

#### `work.discover`

Liệt kê feature và ticket có thể hành động theo role.

#### `feature.get_context`

Trả feature rollup, subfeature tree, open tickets, blockers, verified behaviors và preservation constraints.

#### `ticket.get_context`

Trả context packet đầy đủ của ticket.

#### `context.resolve`

Resolve một hoặc nhiều typed `context_ref`. Tool phải từ chối reference ngoài tenant/project scope.

#### `events.list`

Đọc event mới sau một cursor để agent hoặc UI đồng bộ trạng thái.

### 6.2. Planner tools

- `planning.create_feature`
- `planning.update_feature`
- `planning.create_ticket`
- `planning.split_ticket`
- `planning.set_dependency`
- `planning.set_priority`
- `planning.define_acceptance_gate`
- `planning.define_preservation_constraints`
- `planning.mark_ready`

Planner không được:

- Sửa production code.
- Submit candidate thay Developer.
- Tạo QA verdict.
- Bỏ qua evidence gate để đóng ticket.

### 6.3. Developer tools

- `work.claim`
- `work.heartbeat`
- `work.release`
- `dev.append_progress`
- `dev.record_baseline`
- `dev.report_local_test`
- `dev.mark_blocked`
- `dev.submit_candidate`

Developer không được:

- Tự đánh dấu QA pass.
- Thay acceptance criteria sau khi claim.
- Ghi ngoài file scope nếu chưa được mở rộng quyền.
- Sửa candidate đã freeze.
- Merge vào protected branch.

### 6.4. QA tools

- `qa.discover_candidates`
- `qa.claim_candidate`
- `qa.append_observation`
- `qa.attach_evidence`
- `qa.record_finding`
- `qa.submit_verdict`
- `qa.reopen_verified_behavior`
- `qa.release_candidate`

QA không được:

- Sửa production code.
- Thay candidate SHA trong một QA run.
- Đánh dấu verified khi không có evidence phù hợp.
- Dùng developer claim làm bằng chứng duy nhất.

### 6.5. Integrator tools

- `integration.discover_ready_candidates`
- `integration.check_mergeability`
- `integration.run_mainline_regression`
- `integration.merge_candidate`
- `integration.record_conflict`
- `integration.requeue`

Integrator chỉ merge candidate đã đạt QA gate và policy gate.

### 6.6. Ví dụ input/output của thao tác claim

Input:

```json
{
  "ticket_id": "DEV-142",
  "expected_version": 17,
  "requested_lease_seconds": 1800
}
```

Output:

```json
{
  "data": {
    "lease_id": "lease_01K4...",
    "ticket_id": "DEV-142",
    "agent_id": "dev-agent-03",
    "expires_at": "2026-09-09T10:30:00Z",
    "workspace": {
      "base_commit": "7f42c1a",
      "branch": "agent/dev-agent-03/DEV-142",
      "worktree_ref": "wt:DEV-142:a91d20e"
    },
    "context_snapshot_id": "ctxsnap_01K4..."
  }
}
```

Conflict response khi ticket đã được claim:

```json
{
  "error": {
    "code": "lease_conflict",
    "message": "Ticket is already leased by another agent",
    "details": {
      "retry_after_seconds": 420
    }
  }
}
```

## 7. Context packet theo vai trò

### 7.1. Context chung

Mọi context packet có:

- `context_snapshot_id`
- `generated_at`
- `project_id`
- `feature_path`
- `ticket`
- `source_refs[]`
- `context_version`
- `token_budget`
- `omitted_sections[]`

### 7.2. Developer context

Ưu tiên:

1. Objective và acceptance criteria.
2. Base commit và workspace identity.
3. Known failure cùng reproduction evidence.
4. Preservation constraints.
5. Dependency đã hoàn thành.
6. File/component liên quan.
7. Design decision còn hiệu lực.
8. Giới hạn scope và thao tác cấm.

Không ưu tiên đưa vào:

- Toàn bộ chat lịch sử.
- QA rubric bí mật.
- Ticket không liên quan.
- Output dài không có tóm tắt hoặc provenance.

### 7.3. QA context

Ưu tiên:

1. Candidate identity bất biến.
2. Product requirement và acceptance criteria.
3. Preservation constraints cần regression test.
4. Test environment và public test interface.
5. Known risks hoặc failure cũ cần tái kiểm tra.
6. Developer evidence để tham khảo, không mặc định tin cậy.

QA không cần toàn bộ reasoning hoặc chat của Developer.

### 7.4. Planner context

Ưu tiên:

1. Product specification.
2. Feature tree và dependency graph.
3. Verified behavior.
4. Open, failed, flaky và insufficient-evidence finding.
5. Agent/workload capacity.
6. Mainline health và conflict risk.
7. Context freshness.

### 7.5. Context compaction

Khi context vượt budget:

1. Giữ nguyên acceptance criteria và security constraints.
2. Giữ evidence gần nhất của bug đang xử lý.
3. Giữ quyết định chưa bị superseded.
4. Tóm tắt progress cũ thành checkpoint.
5. Thay log dài bằng summary và storage reference.
6. Ghi rõ phần nào đã bị lược bỏ.

Không được tóm tắt làm thay đổi trạng thái verified/failed hoặc làm mất candidate SHA.

## 8. State machine

```text
BACKLOG
  -> PLANNED
  -> READY
  -> DEV_CLAIMED
  -> DEV_IN_PROGRESS
  -> QA_READY
  -> QA_CLAIMED
  -> QA_IN_PROGRESS
      -> REWORK -> READY
      -> VERIFIED -> MERGE_READY
  -> INTEGRATING
      -> REWORK
      -> DONE
```

Các trạng thái phụ:

- `BLOCKED`: cần dependency, môi trường hoặc quyết định.
- `CANCELLED`: không còn nằm trong scope.
- `SUPERSEDED`: bị thay thế bởi ticket khác.

### 8.1. Transition guard tối thiểu

| Transition | Điều kiện |
|---|---|
| `PLANNED -> READY` | Có objective, acceptance criteria, required role và dependency hợp lệ |
| `READY -> DEV_CLAIMED` | Agent có quyền, không có lease active, dependency đã ready |
| `DEV_IN_PROGRESS -> QA_READY` | Candidate SHA tồn tại, build policy đạt, candidate đã freeze |
| `QA_READY -> QA_CLAIMED` | QA agent khác developer nếu policy yêu cầu, candidate không đổi |
| `QA_IN_PROGRESS -> VERIFIED` | Tất cả criterion bắt buộc có finding verified và evidence |
| `QA_IN_PROGRESS -> REWORK` | Có failed hoặc insufficient-evidence finding bắt buộc |
| `VERIFIED -> MERGE_READY` | Candidate vẫn dựa trên base hợp lệ hoặc rebase check đạt |
| `INTEGRATING -> DONE` | Merge thành công và mainline regression đạt |

Mọi write request gửi `expected_version`. Nếu version đã đổi, backend trả conflict thay vì ghi đè.

## 9. Phân quyền

### 9.1. Permission matrix

| Resource/action | Planner | Developer | QA | Integrator |
|---|---:|---:|---:|---:|
| Đọc project/feature metadata | Có | Có theo scope | Có theo scope | Có |
| Tạo/sửa feature tree | Có | Không | Không | Không |
| Tạo ticket và dependency | Có | Đề xuất | Tạo bug/finding liên kết | Requeue integration |
| Claim dev ticket | Không | Có | Không | Không |
| Sửa artifact | Không | Trong worktree được lease | Không | Chỉ thao tác integration |
| Submit candidate | Không | Có | Không | Không |
| Đọc candidate | Có | Candidate của mình | Có, read-only | Có |
| Ghi evidence | Không | Developer-local evidence | QA evidence | Integration evidence |
| Submit QA verdict | Không | Không | Có | Không |
| Merge protected branch | Không | Không | Không | Có khi đủ gate |
| Đổi policy/role | Không | Không | Không | Không; chỉ admin/human |

### 9.2. Authorization model

Quyền thực tế là giao của:

```text
effective_permissions = role_permissions
                      ∩ project_scope
                      ∩ ticket_scope
                      ∩ active_lease_scope
                      ∩ policy_constraints
```

Không tin role do client gửi trong request. Role và scope phải lấy từ authenticated identity.

### 9.3. Bảo vệ dữ liệu và tool

- Token ngắn hạn, có audience và project scope.
- Secret không xuất hiện trong ticket context hoặc log.
- Tất cả input validate bằng schema.
- Storage reference dùng allowlist và signed access ngắn hạn.
- File path được canonicalize và kiểm tra nằm trong worktree được phép.
- Không thực thi shell command lấy trực tiếp từ metadata hoặc ticket text.
- Evidence upload kiểm tra loại file, kích thước và checksum.
- Rate limit riêng cho discovery, context build và upload evidence.
- Audit log append-only.

## 10. Local skill cho từng AI

Mỗi AI worker được cài một base skill và một role skill. Skill không chứa secret và không hardcode endpoint production.

```text
skills/
├── control-plane-base/
│   ├── SKILL.md
│   └── references/
│       ├── context-contract.md
│       ├── error-handling.md
│       └── evidence-format.md
├── planner-agent/
│   ├── SKILL.md
│   └── references/planning-standard.md
├── developer-agent/
│   ├── SKILL.md
│   └── references/development-standard.md
├── qa-agent/
│   ├── SKILL.md
│   └── references/qa-standard.md
└── integrator-agent/
    ├── SKILL.md
    └── references/integration-standard.md
```

### 10.1. Base skill

Quy định chung:

- Cách xác minh identity và capability.
- Cách gọi discovery.
- Cách resolve context reference.
- Cách xử lý stale context và version conflict.
- Cách heartbeat/release lease.
- Cách ghi progress có provenance.
- Cách dừng an toàn khi thiếu quyền hoặc context.

### 10.2. Planner standard

- Mỗi ticket phải bounded nhưng locally complete.
- Acceptance criteria phải quan sát được.
- Blocker/regression được ưu tiên trước mở rộng tính năng.
- Không chia task chỉ dựa vào số file.
- Ghi rõ preservation constraints.
- Không sửa code hoặc tự đánh giá QA.

### 10.3. Developer standard

- Đọc base commit và xác minh worktree trước khi sửa.
- Chạy baseline trước thay đổi khi có thể.
- Chỉ làm trong ticket scope.
- Test sau mỗi thay đổi có ý nghĩa.
- Không tuyên bố hoàn tất thay cho QA.
- Submit candidate cùng test evidence và known limitations.
- Khi bị kẹt, ghi blocker cụ thể và release lease nếu không còn hành động hữu ích.

### 10.4. QA standard

- Xác nhận candidate SHA trước khi test.
- Derive test từ requirement và acceptance criteria.
- Kết hợp black-box và white-box khi phù hợp.
- Mỗi verdict phải có evidence reference.
- Phân biệt `failed`, `flaky` và `insufficient_evidence`.
- Không sửa production code.
- Khi phát hiện regression, liên kết behavior từng verified trước đó.

### 10.5. Integrator standard

- Chỉ nhận candidate đã verified.
- Kiểm tra base drift, conflict và mainline health.
- Chạy regression cần thiết sau merge/rebase.
- Không bỏ qua failed gate.
- Nếu integration làm candidate thay đổi, chuyển lại QA thay vì giữ verdict cũ.

### 10.6. Skill versioning

Agent registration lưu:

- `skill_name`
- `skill_version`
- `skill_checksum`
- `compatible_protocol_version`
- `loaded_at`

`identity.get_capabilities` phải từ chối hoặc giảm quyền agent có skill version không tương thích với protocol hiện tại.

## 11. Web UI

UI là operational console, không chỉ là bảng ticket.

### 11.1. Project overview

Hiển thị:

- Feature đang active.
- Ticket ready, in development, awaiting QA, blocked, rework.
- Mainline health.
- Candidate đang chờ integration.
- Agent online và lease sắp hết hạn.

### 11.2. Feature explorer

Hiển thị cây:

```text
Feature
├── Subfeature A
│   ├── DEV-101 verified
│   └── BUG-118 rework
└── Subfeature B
    └── DEV-124 blocked by OPS-19
```

Khi chọn feature, UI hiển thị:

- Objective.
- Progress rollup.
- Context version.
- Open gaps.
- Verified behaviors.
- Preservation constraints.
- Ticket theo role đang chờ nhận.

### 11.3. Kanban board

Các cột mặc định:

- Ready
- In development
- Awaiting QA
- QA in progress
- Rework
- Merge ready
- Blocked
- Done

Card tối thiểu có:

- Ticket ID và title.
- Feature path.
- Required role.
- Agent/lease.
- Dependency count.
- Candidate SHA nếu có.
- Evidence/finding count.
- Blocker hoặc QA status.

### 11.4. Ticket detail

Các tab:

1. `Context`: objective, acceptance, preservation, dependencies.
2. `Activity`: event timeline append-only.
3. `Code`: base/candidate commit, branch/worktree, changed files.
4. `Evidence`: tests, log, trace, screenshot, video.
5. `QA`: criteria matrix, findings và verdict.
6. `Agent`: lease, heartbeat, skill version và capability.

### 11.5. Agent operations

Hiển thị:

- Agent identity và role.
- Online/offline/stale.
- Ticket đang giữ.
- Lease còn lại.
- Skill và protocol version.
- Context snapshot đã nhận.
- Lần gọi MCP gần nhất.
- Error hoặc permission denial gần nhất.

Không hiển thị secret hoặc raw token.

### 11.6. Evidence ledger

Cho phép lọc theo:

- Project/feature/ticket.
- Candidate SHA.
- Criterion.
- Evidence type.
- QA status.
- Environment.
- Agent.
- Thời gian.

Một QA verdict phải mở được toàn bộ evidence hỗ trợ và provenance chain.

### 11.7. Policy và skill registry

Hiển thị read-only cho người dùng thường và editable cho admin:

- Role permission matrix.
- Protected branch policy.
- Required QA gates.
- Lease duration.
- Skill version được chấp nhận.
- Protocol compatibility.
- Evidence retention policy.

### 11.8. Đồng bộ gần thời gian thực

UI nhận domain event qua SSE hoặc WebSocket. REST/UI API vẫn là nơi đọc snapshot và thực hiện command; event stream chỉ thông báo thay đổi.

Client phải xử lý reconnect bằng event cursor và tải lại snapshot khi cursor quá cũ.

## 12. REST/UI API

MCP và UI dùng chung application service nhưng có adapter riêng.

Ví dụ resource API:

```text
GET    /api/v1/projects
GET    /api/v1/projects/{projectId}/features
GET    /api/v1/features/{featureId}
GET    /api/v1/features/{featureId}/tickets
GET    /api/v1/tickets/{ticketId}
GET    /api/v1/tickets/{ticketId}/context
GET    /api/v1/tickets/{ticketId}/events
POST   /api/v1/tickets/{ticketId}/leases
POST   /api/v1/tickets/{ticketId}/candidates
POST   /api/v1/candidates/{candidateId}/qa-runs
POST   /api/v1/qa-runs/{qaRunId}/findings
POST   /api/v1/qa-runs/{qaRunId}/verdict
GET    /api/v1/evidence
GET    /api/v1/agents
GET    /api/v1/events?cursor=...
```

Quy ước:

- Dùng noun số nhiều cho resource.
- Cursor pagination cho events, evidence và activity.
- `409 Conflict` cho version/lease/state conflict.
- `422 Unprocessable Entity` cho transition không thỏa guard.
- `401` khi chưa xác thực; `403` khi có identity nhưng thiếu quyền.
- Error trả stable error code, không trả stack trace.
- Mọi command hỗ trợ idempotency key.

## 13. Storage schema ban đầu

Các bảng chính:

```text
projects
project_repositories
features
tickets
ticket_dependencies
ticket_acceptance_criteria
context_entries
context_snapshots
candidates
test_runs
evidence
qa_findings
qa_verdicts
leases
agents
agent_capabilities
skills
agent_skill_assignments
policy_profiles
audit_events
outbox_events
```

Nguyên tắc:

- ID không đoán được và unique toàn hệ thống.
- Tất cả bảng mutable có `version` cho optimistic concurrency.
- Audit/event dùng timestamp server.
- `candidate.commit_sha` bất biến.
- Evidence có checksum.
- Foreign key ngăn evidence trỏ tới candidate không tồn tại.
- Không hard-delete record có liên quan audit; dùng retention/archival policy.

## 14. Domain events

Danh sách sự kiện MVP:

```text
project.created
feature.created
feature.updated
ticket.created
ticket.ready
ticket.claimed
ticket.lease_renewed
ticket.lease_expired
ticket.progress_appended
ticket.blocked
ticket.unblocked
candidate.submitted
candidate.frozen
qa.started
qa.evidence_attached
qa.finding_recorded
qa.verdict_submitted
ticket.rework_requested
ticket.verified
integration.started
integration.conflict_detected
integration.completed
ticket.done
ticket.reopened
context.superseded
agent.heartbeat
agent.permission_denied
```

Mỗi event gồm:

- `event_id`
- `event_type`
- `aggregate_type`
- `aggregate_id`
- `aggregate_version`
- `actor_id`
- `actor_role`
- `project_id`
- `occurred_at`
- `correlation_id`
- `causation_id`
- Payload đã validate

## 15. Jira và GitHub integration

### 15.1. Khuyến nghị

Không dùng Jira làm database lõi ở giai đoạn đầu. Jira phù hợp làm giao diện tổ chức và đồng bộ ticket, nhưng khó biểu diễn đầy đủ candidate identity, lease transaction, evidence provenance và context snapshot.

### 15.2. Adapter hai chiều

- Control plane tạo hoặc cập nhật issue Jira.
- Jira webhook tạo command được validate trong control plane.
- Lưu `external_system`, `external_project_key`, `external_issue_key`.
- Dùng idempotency key để tránh vòng lặp sync.
- Chỉ một hệ thống làm authority cho từng field.

Ví dụ:

| Field | Authority |
|---|---|
| Title, human priority | Jira hoặc control plane theo cấu hình |
| Workflow state | Control plane |
| Lease | Control plane |
| Candidate SHA | Control plane/Git |
| QA evidence | Control plane |
| Human comment | Đồng bộ hai chiều |

GitHub integration dùng cho commit, pull request, status check và branch protection. Không xem nội dung PR description là verified evidence nếu chưa có test hoặc observation tương ứng.

## 16. Workflow end-to-end

### 16.1. Lập kế hoạch

1. Planner discovery các feature có gap hoặc blocker.
2. Planner resolve feature context.
3. Planner tạo hoặc cập nhật subfeature/ticket.
4. Planner đặt dependency, acceptance và preservation constraints.
5. Backend kiểm tra ticket đủ điều kiện rồi chuyển `READY`.

### 16.2. Phát triển

1. Dev agent đọc capability.
2. Dev gọi `work.discover` với `dependency_ready=true`.
3. Dev đọc feature context rồi ticket context.
4. Dev claim ticket bằng expected version.
5. Backend cấp lease và worktree identity.
6. Dev chạy baseline, sửa code và test cục bộ.
7. Dev append progress/checkpoint.
8. Dev submit candidate commit SHA.
9. Backend freeze candidate và chuyển ticket sang `QA_READY`.

### 16.3. QA

1. QA discovery candidate đang chờ.
2. QA claim candidate.
3. QA xác minh SHA và environment.
4. QA chạy test theo acceptance và preservation gate.
5. QA attach evidence và ghi finding cho từng criterion.
6. Nếu pass, ticket chuyển `VERIFIED`.
7. Nếu fail, ticket chuyển `REWORK`; bug/finding giữ liên kết candidate và evidence.

### 16.4. Integration

1. Integrator nhận candidate verified.
2. Kiểm tra base drift và conflict.
3. Rebase/merge theo policy.
4. Nếu artifact thay đổi, chạy lại gate cần thiết.
5. Chạy mainline regression.
6. Merge và đóng ticket hoặc requeue khi thất bại.

### 16.5. Regression và reopen

Khi behavior từng verified bị fail:

- Tạo `ticket.reopened` hoặc bug liên kết ticket gốc.
- Giữ evidence xác minh cũ và evidence thất bại mới.
- Feature rollup đổi verified behavior thành active preservation failure.
- Planner ưu tiên regression trước feature extension theo policy mặc định.

## 17. Concurrency và chống xung đột

### 17.1. Dependency readiness

Ticket chỉ discoverable cho Dev khi tất cả dependency bắt buộc ở trạng thái phù hợp.

### 17.2. File scope collision

Control plane so sánh `file_scope` giữa các lease active:

- Không giao nhau: cho phép chạy song song.
- Có giao nhau nhẹ: cảnh báo và yêu cầu Integrator policy.
- Giao nhau trực tiếp: từ chối claim hoặc serialize.

### 17.3. Optimistic concurrency

Mọi aggregate mutable có version. Request gửi `expected_version`; mismatch trả `409 state_conflict` kèm version hiện tại và context cần refresh.

### 17.4. Idempotency

Các thao tác claim, submit candidate, submit verdict và merge nhận `idempotency_key` để retry an toàn khi mất kết nối.

### 17.5. Stale context

Context snapshot ghi source versions. Trước thao tác quan trọng, server kiểm tra các phần bắt buộc còn mới. Nếu stale, trả `context_stale` và yêu cầu agent refresh thay vì tiếp tục bằng context cũ.

## 18. Observability

Metrics MVP:

- Ticket lead time và cycle time.
- Thời gian chờ Dev, QA và Integrator.
- Lease expiration/recovery count.
- Rework rate.
- Reopen/regression rate.
- QA evidence completeness.
- Context build latency và token size.
- Context stale/conflict count.
- Agent success/failure theo skill version.
- Candidate pass rate.
- Mainline health.

Trace một workflow bằng `correlation_id` từ discovery tới merge. Log phải redact token, secret và thông tin nhạy cảm.

## 19. Cấu trúc repository đề xuất

```text
hoh-control-plane/
├── apps/
│   ├── api/                 # REST/UI API
│   └── web/                 # Kanban và operational console
├── services/
│   ├── mcp-gateway/         # Metadata + role MCP adapter
│   ├── context-engine/      # Context compiler và snapshot
│   ├── event-dispatcher/    # Outbox/event delivery
│   └── integration-worker/  # Git/Jira/GitHub adapter
├── packages/
│   ├── domain/              # State machine và invariant
│   ├── contracts/           # Shared schemas
│   ├── authorization/       # RBAC/ABAC policy
│   └── observability/
├── skills/
│   ├── control-plane-base/
│   ├── planner-agent/
│   ├── developer-agent/
│   ├── qa-agent/
│   └── integrator-agent/
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   ├── security/
│   └── e2e/
├── docs/
│   ├── architecture/
│   ├── adr/
│   └── protocol/
└── deploy/
```

## 20. Lộ trình hiện thực

### Phase 0 - Domain prototype

Xây:

- Project/feature/ticket model.
- State machine.
- Permission matrix.
- Lease và optimistic concurrency.
- Audit event.

Điều kiện hoàn thành:

- Không thể thực hiện transition trái phép.
- Hai Dev không claim cùng ticket.
- QA không thể sửa artifact hoặc submit candidate.
- Developer không thể submit QA verdict.

### Phase 1 - Metadata và context MCP

Xây:

- `identity.get_capabilities`
- `work.discover`
- `feature.get_context`
- `ticket.get_context`
- Context snapshot và provenance.

Điều kiện hoàn thành:

- Một agent mới có thể tìm đúng ticket bằng role mà không đọc toàn database.
- Mọi context packet có version, source ref và token budget.
- Typed reference ngoài project bị từ chối.

### Phase 2 - Developer workflow

Xây:

- Claim/heartbeat/release.
- Worktree adapter.
- Progress checkpoint.
- Candidate submit/freeze.

Điều kiện hoàn thành:

- Dev đi từ discovery tới candidate hoàn toàn qua MCP.
- Candidate luôn có SHA bất biến.
- Lease hết hạn có thể recover.

### Phase 3 - QA và evidence

Xây:

- QA candidate queue.
- Evidence upload/metadata.
- Finding theo criterion.
- Verdict và rework.

Điều kiện hoàn thành:

- QA không pass nếu criterion bắt buộc thiếu evidence.
- Evidence không thể gắn nhầm candidate.
- Failed verdict tạo rework context cho Dev tiếp theo.

### Phase 4 - Web UI

Xây:

- Project overview.
- Feature explorer.
- Kanban.
- Ticket detail.
- Agent/lease monitor.
- Evidence ledger.
- Event updates gần thời gian thực.

Điều kiện hoàn thành:

- Người dùng truy vết được từ feature tới ticket, candidate, evidence và verdict.
- UI thể hiện stale/blocked/lease-expiring rõ ràng.
- Không có thao tác UI nào bypass policy engine.

### Phase 5 - Local skills

Xây base skill và bốn role skill cùng contract tests.

Điều kiện hoàn thành:

- Mỗi role tuân thủ đúng discovery loop.
- Skill version không tương thích bị phát hiện.
- Dev và QA tạo handoff đúng schema.

### Phase 6 - Integrations

Thêm GitHub trước, Jira sau:

- Commit/PR/status check.
- Protected branch integration.
- Jira issue sync và webhook.
- Idempotent reconciliation job.

## 21. Chiến lược kiểm thử

### Unit tests

- State transition guard.
- Permission evaluation.
- Context ranking/compaction.
- Lease expiration.
- Feature progress rollup.

### Contract tests

- MCP input/output schema.
- Stable error code.
- Skill/protocol compatibility.
- REST và MCP cùng tạo kết quả domain giống nhau.

### Integration tests

- PostgreSQL transaction và outbox.
- Git worktree/candidate freeze.
- Object storage checksum.
- Jira/GitHub idempotent sync.

### Security tests

- Cross-project context access bị từ chối.
- Role escalation bị từ chối.
- Path traversal khỏi worktree bị từ chối.
- Metadata chứa lệnh độc hại không được thực thi.
- Evidence upload sai loại/kích thước bị từ chối.
- Secret bị redact khỏi log và context.
- Replay idempotency key không tạo bản ghi trùng.

### End-to-end scenario

1. Planner tạo feature, subfeature và ticket.
2. Dev discovery, claim, submit candidate.
3. QA fail một criterion và attach evidence.
4. Dev khác nhận rework context.
5. QA pass candidate mới.
6. Integrator merge.
7. Regression test mainline fail và reopen ticket.
8. UI hiển thị đầy đủ chuỗi provenance.

## 22. MVP deployment

Khuyến nghị bắt đầu bằng một modular monolith:

- Một API/control-plane process.
- Một MCP gateway process hoặc module.
- Một Web UI.
- PostgreSQL.
- S3-compatible object storage.
- Git provider/local Git adapter.

Chưa cần microservice hoặc message broker ở MVP. Dùng transaction + outbox để giữ tính nhất quán. Tách service khi đã có nhu cầu scale hoặc isolation đo được.

## 23. Các quyết định mặc định

| Câu hỏi | Quyết định khuyến nghị |
|---|---|
| Một hay nhiều MCP server? | Một backend, nhiều role capability profile |
| Jira có làm source of truth? | Không; Jira là adapter/UI integration |
| Context lưu dạng vector-only? | Không; structured records là chính, semantic search là phụ |
| Candidate identity | Git commit SHA hoặc immutable artifact digest |
| Scheduler dùng LLM? | Không cho core state transition; dùng deterministic policy |
| QA có sửa code? | Không |
| Agent có merge main? | Chỉ Integrator với policy gate |
| Parallelism | Chỉ khi dependency và write scope cho phép |
| Kiến trúc MVP | Modular monolith + PostgreSQL + object storage |
| UI realtime | SSE trước; WebSocket khi có nhu cầu hai chiều thực sự |

## 24. Phiên bản MVP nhỏ nhất có giá trị

MVP đầu tiên chỉ cần chứng minh một luồng:

```text
Planner tạo ticket
-> Dev discovery và claim
-> Dev submit immutable candidate
-> QA discovery và fail/pass bằng evidence
-> Dev nhận lại context rework
-> QA pass candidate mới
-> Kanban và ticket detail cập nhật theo event
```

Không cần Jira, nhiều repository, semantic vector search hoặc autoscaling agent ở bản đầu. Nếu luồng trên chưa chặt, thêm integration chỉ làm hệ thống khó debug hơn.

## 25. Tiêu chí thành công của hệ thống

Hệ thống đạt mục tiêu khi:

1. Một AI mới khởi động có thể tự tìm đúng phần việc từ Metadata MCP.
2. Agent chỉ nhận context cần thiết và biết nguồn của từng phần context.
3. Không có hai agent vô tình cùng ghi lên một ticket/worktree scope.
4. Không có QA verdict gắn với artifact không xác định.
5. Một bug chưa xong luôn discoverable cho role phù hợp.
6. Verified behavior được truyền thành preservation constraint cho thay đổi sau.
7. Con người truy vết được toàn bộ quá trình qua UI.
8. Role permission được backend cưỡng chế, không phụ thuộc prompt obedience.
9. Agent thay thế có thể tiếp tục công việc từ ticket context mà không cần chat history của agent trước.
10. Jira/GitHub có thể thay thế hoặc tắt mà không làm mất workflow state cốt lõi.
