-- 多渠道撤回与替换：传播清单、处置通知、结果凭证、升级路径、放行凭证、再传播发现、审计与幂等。

CREATE TABLE IF NOT EXISTS materials (
    material_ref TEXT PRIMARY KEY,
    parent_ref TEXT REFERENCES materials(material_ref),
    derivative_kind TEXT NOT NULL DEFAULT 'original',   -- original | cropped | reworded | reformatted
    material_class TEXT NOT NULL DEFAULT 'campaign',    -- campaign | packaging | endorsement | formula | consumer
    fingerprint_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',              -- active | withdrawn
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_materials_fingerprint ON materials(fingerprint_sha256);

CREATE TABLE IF NOT EXISTS propagation_entries (
    entry_ref TEXT PRIMARY KEY,
    material_ref TEXT NOT NULL REFERENCES materials(material_ref),
    channel_code TEXT NOT NULL,                         -- self_store | dealer_video | offline_screen | endorser_team ...
    account_ref TEXT NOT NULL,                          -- 投放账号
    channel_owner_ref TEXT NOT NULL,                    -- 渠道负责人（经销商只能看到自己的点位）
    locations_json TEXT NOT NULL,                       -- 可核验展示位置：[url/屏幕编号/门店点位...]
    discovered_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'listed'               -- listed | cleared
);

CREATE TABLE IF NOT EXISTS recall_cases (
    case_ref TEXT PRIMARY KEY,
    risk_notice_ref TEXT NOT NULL,                      -- 最初风险通知编号（追溯起点）
    owner_ref TEXT NOT NULL,                            -- 风险负责人
    reason TEXT NOT NULL,
    deadline_at TEXT NOT NULL,                          -- 撤回期限
    ack_deadline_at TEXT NOT NULL,                      -- 签收期限（逾期升级为“无法联系”）
    requires_replacement INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',                -- open | closed
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_materials (
    case_ref TEXT NOT NULL REFERENCES recall_cases(case_ref),
    material_ref TEXT NOT NULL REFERENCES materials(material_ref),
    PRIMARY KEY (case_ref, material_ref)
);

CREATE TABLE IF NOT EXISTS disposal_tasks (
    notice_no TEXT PRIMARY KEY,                         -- 处置通知唯一编号
    case_ref TEXT NOT NULL REFERENCES recall_cases(case_ref),
    entry_ref TEXT NOT NULL REFERENCES propagation_entries(entry_ref),
    assignee_ref TEXT NOT NULL,                         -- 渠道负责人
    required_locations_json TEXT NOT NULL,              -- 需清理位置快照
    deadline_at TEXT NOT NULL,
    ack_deadline_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'issued',              -- issued | acknowledged | in_progress | escalated | closed
    replacement_material_ref TEXT REFERENCES materials(material_ref),
    replacement_credential_ref TEXT,
    closed_at TEXT,
    close_summary TEXT,
    task_seq INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tasks_case ON disposal_tasks(case_ref);
CREATE INDEX IF NOT EXISTS ix_tasks_assignee ON disposal_tasks(assignee_ref);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_ref TEXT PRIMARY KEY,
    notice_no TEXT NOT NULL REFERENCES disposal_tasks(notice_no),
    evidence_type TEXT NOT NULL,                        -- platform_callback | manual_screenshot | field_recheck
    submitted_by TEXT NOT NULL,
    callback_id TEXT,                                   -- 平台回调幂等键
    locations_cleared_json TEXT NOT NULL DEFAULT '[]',
    attachment_sha256 TEXT,
    is_duplicate INTEGER NOT NULL DEFAULT 0,            -- 重复回调：记录但不计入关闭条件
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_evidence_notice ON evidence(notice_no);

CREATE TABLE IF NOT EXISTS escalations (
    escalation_ref TEXT PRIMARY KEY,
    notice_no TEXT NOT NULL REFERENCES disposal_tasks(notice_no),
    kind TEXT NOT NULL,                                 -- unreachable | refused | reappeared | partial_replacement
    status TEXT NOT NULL DEFAULT 'open',                -- open | resolved
    detail TEXT,
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_escalations_notice ON escalations(notice_no);

CREATE TABLE IF NOT EXISTS release_credentials (
    credential_ref TEXT PRIMARY KEY,
    material_ref TEXT NOT NULL REFERENCES materials(material_ref),
    reviewer_ref TEXT NOT NULL,                         -- 独立审核人（须不同于风险负责人）
    issued_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'valid'                -- valid | revoked
);

CREATE TABLE IF NOT EXISTS sightings (
    sighting_ref TEXT PRIMARY KEY,
    fingerprint_sha256 TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    channel_code TEXT,
    account_ref TEXT,
    location_ref TEXT,
    reporter_ref TEXT NOT NULL,
    matched_material_ref TEXT,
    matched_case_ref TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_ref TEXT,
    notice_no TEXT,
    actor_ref TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_case ON audit_events(case_ref);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    source_system TEXT NOT NULL,
    source_seq TEXT NOT NULL,
    action TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_system, source_seq, action)
);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('002_recall');
