-- 多渠道撤回与替换后端
-- 一次风险事件(incident)把原始素材、派生件、投放账号、渠道负责人与可核验点位
-- 组成传播清单；风险负责人圈定范围后逐通知(notice)处置，凭证(evidence)核验通过
-- 才允许闭环；四类异常进入升级(escalation)；替代素材必须持有独立放行凭证。

PRAGMA foreign_keys = ON;

-- 访问主体：全部为受控引用编号，不存真实身份。token 仅用于演示环境。
CREATE TABLE actors (
    actor_id     TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (
                     role IN ('risk_owner','legal','compliance','field_auditor',
                              'executive','dealer','talent','system')),
    org_type     TEXT CHECK (org_type IN ('brand','dealer','talent')),
    org_id       TEXT,
    api_token    TEXT NOT NULL UNIQUE
);

-- 渠道字典与负责人
CREATE TABLE channels (
    channel_id      TEXT PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    owner_actor_id  TEXT REFERENCES actors(actor_id),
    owner_org_type  TEXT NOT NULL CHECK (owner_org_type IN ('brand','dealer','talent')),
    owner_org_id    TEXT NOT NULL
);

-- 投放账号（直营网店、经销商短视频号、线下屏幕运营方、明星团队账号）
CREATE TABLE accounts (
    account_id  TEXT PRIMARY KEY,
    channel_id  TEXT NOT NULL REFERENCES channels(channel_id),
    account_ref TEXT NOT NULL,          -- 受控引用
    org_type    TEXT NOT NULL CHECK (org_type IN ('brand','dealer','talent')),
    org_id      TEXT NOT NULL,
    UNIQUE (channel_id, account_ref)
);

-- 风险事件
CREATE TABLE incidents (
    incident_id     TEXT PRIMARY KEY,
    ref             TEXT NOT NULL UNIQUE,           -- INC-...
    title           TEXT NOT NULL,
    product_ref     TEXT NOT NULL,
    formula_revision INTEGER,                        -- 敏感：代言团队不可见
    risk_type       TEXT NOT NULL,                  -- 如 mistaken_medicinal_claim
    source_system   TEXT NOT NULL,                  -- 最初风险通知来源系统
    source_sequence TEXT NOT NULL,                  -- 来源序列号（来源责任边界）
    detail_ref      TEXT,                           -- 受控附件引用
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','monitoring','closed')),
    risk_owner_id   TEXT NOT NULL REFERENCES actors(actor_id),
    created_at      TEXT NOT NULL,
    closed_at       TEXT
);

-- 素材：原始件、裁剪/改字派生件、替代件
CREATE TABLE assets (
    asset_id             TEXT PRIMARY KEY,
    ref                  TEXT NOT NULL UNIQUE,     -- MAT-...
    incident_id          TEXT NOT NULL REFERENCES incidents(incident_id),
    kind                 TEXT NOT NULL CHECK (kind IN ('original','derivative','replacement')),
    derived_from_asset_id TEXT REFERENCES assets(asset_id),
    sha256               TEXT NOT NULL,
    transform_note       TEXT,                      -- 裁剪/改字说明
    consumer_material    INTEGER NOT NULL DEFAULT 0, -- 消费者材料：代言团队不可见
    status               TEXT NOT NULL DEFAULT 'in_circulation'
                         CHECK (status IN ('in_circulation','withdrawn','replacement')),
    created_at           TEXT NOT NULL,
    UNIQUE (incident_id, sha256)
);

-- 可核验展示位置（一条视频、一个商品页、一块屏幕的一个播放位……）
CREATE TABLE placements (
    incident_id       TEXT NOT NULL REFERENCES incidents(incident_id),
    placement_id      TEXT PRIMARY KEY,
    ref               TEXT NOT NULL UNIQUE,         -- PL-...
    asset_id          TEXT NOT NULL REFERENCES assets(asset_id),
    channel_id        TEXT NOT NULL REFERENCES channels(channel_id),
    account_id        TEXT REFERENCES accounts(account_id),
    locator           TEXT NOT NULL,                 -- URL/屏幕编号/页面路径，可核验
    owner_org_type    TEXT NOT NULL CHECK (owner_org_type IN ('brand','dealer','talent')),
    owner_org_id      TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'identified'
                      CHECK (status IN ('identified','scoped','notified','resolved','recurred')),
    required_action   TEXT CHECK (required_action IN ('takedown','replace')),
    replacement_asset_id TEXT REFERENCES assets(asset_id),
    deadline          TEXT,
    round             INTEGER NOT NULL DEFAULT 0,    -- 通知轮次，复发递增
    current_notice_id TEXT,
    first_seen_at     TEXT NOT NULL,
    notified_at       TEXT,
    resolved_at       TEXT,
    last_observed_at  TEXT,
    overdue_flag      INTEGER NOT NULL DEFAULT 0
);

-- 处置通知：一次范围圈定生成一个带唯一编号的通知，覆盖多个点位
CREATE TABLE notices (
    notice_id        TEXT PRIMARY KEY,
    ref              TEXT NOT NULL UNIQUE,           -- N-INC...-PL...-01
    incident_id      TEXT NOT NULL REFERENCES incidents(incident_id),
    channel_id       TEXT NOT NULL REFERENCES channels(channel_id),
    recipient_actor_id TEXT REFERENCES actors(actor_id),
    required_action  TEXT NOT NULL CHECK (required_action IN ('takedown','replace')),
    round            INTEGER NOT NULL DEFAULT 1,
    delivery_channel TEXT NOT NULL,                  -- email / platform_api
    issued_at        TEXT NOT NULL,
    due_at           TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'issued'
                     CHECK (status IN ('issued','delivered','failed','superseded','completed')),
    contact_attempts INTEGER NOT NULL DEFAULT 0,
    completed_at     TEXT
);

CREATE TABLE notice_placements (
    notice_id    TEXT NOT NULL REFERENCES notices(notice_id),
    placement_id TEXT NOT NULL REFERENCES placements(placement_id),
    PRIMARY KEY (notice_id, placement_id)
);

-- 结果凭证：平台回调 / 人工截图摘要 / 现场复核 / 联系结果
CREATE TABLE evidences (
    evidence_id   TEXT PRIMARY KEY,
    ref           TEXT NOT NULL UNIQUE,             -- EV-...
    incident_id   TEXT NOT NULL REFERENCES incidents(incident_id),
    notice_id     TEXT REFERENCES notices(notice_id),
    placement_id  TEXT REFERENCES placements(placement_id),
    kind          TEXT NOT NULL CHECK (kind IN
                  ('platform_callback','manual_screenshot','field_recheck','contact_record')),
    status        TEXT NOT NULL DEFAULT 'pending_verification'
                  CHECK (status IN ('pending_verification','verified','rejected','duplicate')),
    source_system TEXT NOT NULL,
    delivery_id   TEXT,                             -- 回调投递唯一编号（幂等）
    contact_result TEXT CHECK (contact_result IN ('unreachable','refused','acknowledged')),
    claimed_complete INTEGER NOT NULL DEFAULT 0,    -- 提交方是否声称全部完成
    summary       TEXT,                             -- 截图/复核文字摘要
    attachment_sha256 TEXT,                         -- 附件只存摘要
    submitted_by_actor_id TEXT REFERENCES actors(actor_id),
    submitted_at  TEXT NOT NULL,
    verified_by_actor_id TEXT REFERENCES actors(actor_id),
    verified_at   TEXT,
    verify_note   TEXT
);
CREATE UNIQUE INDEX idx_evidence_delivery ON evidences(delivery_id)
    WHERE delivery_id IS NOT NULL;

-- 凭证对单个点位的声称与核验结论
CREATE TABLE evidence_coverage (
    evidence_id          TEXT NOT NULL REFERENCES evidences(evidence_id),
    placement_id         TEXT NOT NULL REFERENCES placements(placement_id),
    claimed_state        TEXT NOT NULL CHECK (claimed_state IN ('removed','replaced','still_present','partial')),
    claimed_replacement_asset_id TEXT REFERENCES assets(asset_id),
    verified_state       TEXT CHECK (verified_state IN ('removed','replaced','mismatch','missing','unverified')),
    PRIMARY KEY (evidence_id, placement_id)
);

-- 升级：无法联系 / 拒绝下架 / 旧图再现 / 只替换部分页面
CREATE TABLE escalations (
    escalation_id TEXT PRIMARY KEY,
    ref           TEXT NOT NULL UNIQUE,             -- ESC-...
    incident_id   TEXT NOT NULL REFERENCES incidents(incident_id),
    notice_id     TEXT REFERENCES notices(notice_id),
    placement_id  TEXT REFERENCES placements(placement_id),
    type          TEXT NOT NULL CHECK (type IN ('unreachable','refused','recurrence','partial_replace')),
    level         INTEGER NOT NULL DEFAULT 1 CHECK (level IN (1,2,3)),
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
    reason        TEXT NOT NULL,
    opened_by_actor_id TEXT REFERENCES actors(actor_id),
    opened_at     TEXT NOT NULL,
    resolved_by_actor_id TEXT REFERENCES actors(actor_id),
    resolved_at   TEXT,
    resolution_note TEXT
);

-- 替代素材的独立审核放行（旧素材撤回不会自动产生此记录）
CREATE TABLE release_approvals (
    approval_id        TEXT PRIMARY KEY,
    ref                TEXT NOT NULL UNIQUE,         -- REL-...
    asset_id           TEXT NOT NULL UNIQUE REFERENCES assets(asset_id),
    decision           TEXT NOT NULL CHECK (decision IN ('approved','rejected','rescinded')),
    reviewer_actor_id  TEXT NOT NULL REFERENCES actors(actor_id),
    review_batch_ref   TEXT NOT NULL,                -- 独立审核批次
    basis_note         TEXT,
    decided_at         TEXT NOT NULL,
    valid_from         TEXT NOT NULL,
    valid_to           TEXT                          -- NULL 表示长期有效
);

-- 持续监测观察（巡检/举报/平台回流）
CREATE TABLE observations (
    observation_id TEXT PRIMARY KEY,
    incident_id    TEXT REFERENCES incidents(incident_id),
    channel_code   TEXT NOT NULL,
    account_ref    TEXT,
    locator        TEXT NOT NULL,
    asset_sha256   TEXT NOT NULL,                    -- 命中的原始件或派生件摘要
    transform_note TEXT,
    source         TEXT NOT NULL CHECK (source IN ('monitor','report','platform_feed')),
    observed_at    TEXT NOT NULL,
    matched_placement_id TEXT REFERENCES placements(placement_id),
    escalation_id  TEXT REFERENCES escalations(escalation_id)
);

-- 全链路事件日志：从最初风险通知到每个点位的实际下架/替换时间
CREATE TABLE event_log (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id     TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    entity_type     TEXT,
    entity_ref      TEXT,
    actor_id        TEXT,
    source_system   TEXT,
    source_sequence TEXT,
    occurred_at     TEXT NOT NULL,
    payload_json    TEXT
);

-- 写接口幂等
CREATE TABLE idempotency (
    idempotency_key TEXT PRIMARY KEY,
    result_ref      TEXT NOT NULL,
    result_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX idx_placements_incident ON placements(incident_id);
CREATE INDEX idx_placements_owner ON placements(owner_org_type, owner_org_id);
CREATE INDEX idx_placements_status ON placements(status);
CREATE INDEX idx_assets_sha ON assets(sha256);
CREATE INDEX idx_notices_incident ON notices(incident_id);
CREATE INDEX idx_evidences_notice ON evidences(notice_id);
CREATE INDEX idx_escalations_incident ON escalations(incident_id, status);
CREATE INDEX idx_event_log_incident ON event_log(incident_id, occurred_at);

-- 演示主体（无真实身份信息）
INSERT INTO actors (actor_id, display_name, role, org_type, org_id, api_token) VALUES
 ('actor-risk-1',  '风险负责人',   'risk_owner',   'brand', 'BRAND-HQ', 'tok-risk-1'),
 ('actor-legal-1', '品牌法务',     'legal',        'brand', 'BRAND-HQ', 'tok-legal-1'),
 ('actor-comp-1',  '合规审核员',   'compliance',   'brand', 'BRAND-HQ', 'tok-comp-1'),
 ('actor-audit-1', '现场复核员',   'field_auditor','brand', 'BRAND-HQ', 'tok-audit-1'),
 ('actor-exec-1',  '管理层',       'executive',    'brand', 'BRAND-HQ', 'tok-exec-1'),
 ('actor-dealer-east', '东区经销商', 'dealer', 'dealer', 'DEALER-EAST', 'tok-dealer-east'),
 ('actor-dealer-west', '西区经销商', 'dealer', 'dealer', 'DEALER-WEST', 'tok-dealer-west'),
 ('actor-talent-1',    '代言团队',   'talent', 'talent', 'TALENT-T1',   'tok-talent-1'),
 ('actor-system',      '监测系统',   'system', 'brand', 'BRAND-HQ', 'tok-system');

INSERT INTO channels (channel_id, code, name, owner_actor_id, owner_org_type, owner_org_id) VALUES
 ('ch-direct', 'direct_store',       '直营网店',       'actor-legal-1', 'brand',  'BRAND-HQ'),
 ('ch-dsv-east', 'dealer_shortvideo', '东区经销商短视频', 'actor-dealer-east', 'dealer', 'DEALER-EAST'),
 ('ch-dsv-west', 'dealer_shortvideo_west', '西区经销商短视频', 'actor-dealer-west', 'dealer', 'DEALER-WEST'),
 ('ch-screen', 'offline_screen',     '线下屏幕',       'actor-legal-1', 'brand',  'BRAND-HQ'),
 ('ch-talent', 'celebrity_team',     '明星团队',       'actor-talent-1', 'talent', 'TALENT-T1');

INSERT INTO schema_migrations(version) VALUES ('002_recall');
