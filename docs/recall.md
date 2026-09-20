# 多渠道撤回与替换

针对“宣传素材被多账号、多渠道转载后，一封下架邮件无法证明市场已停止传播”的问题，
本模块把撤回执行建模为可核验、可追溯、可升级的后端流程。

## 角色与可见性

| 角色 | 说明 |
| --- | --- |
| `risk_owner` | 风险负责人：圈定撤回范围与期限、签发通知、处理升级 |
| `channel_owner` | 渠道负责人（经销商）：仅可见并处理自己的点位 |
| `endorsement_team` | 代言团队：不可见配方（`formula`）与消费者材料（`consumer`） |
| `management` | 管理层：只读看板，实时掌握覆盖率与逾期风险 |
| `reviewer` | 独立审核人：签发替代素材放行凭证 |
| `system` | 外部系统：平台回调等机器来源 |

所有请求需携带 `X-Actor-Ref` / `X-Actor-Role` 头。所有写操作是业务事件，
请求体必须携带 `source_system` 与 `source_seq`，服务端按 `(来源, 序列号, 动作)` 幂等，
重复投递返回首次结果（`deduplicated: true`）。

## 流程

1. **传播清单**：`POST /recall/materials` 登记原始素材与派生件（`cropped`/`reworded` 等，
   派生件必须挂 `parent_ref`）；`POST /recall/propagation-entries` 登记投放账号、
   渠道负责人与可核验展示位置（URL、屏幕编号、门店点位）。
2. **圈定范围**：`POST /recall/cases` 以最初风险通知编号（`risk_notice_ref`）开立事件，
   给定素材范围、撤回期限与签收期限；范围内素材随即标记为 `withdrawn`。
3. **签发通知**：`POST /recall/cases/{case}/issue-notices` 按清单逐点位生成
   带唯一编号（`DSP-{case}-{seq}`）的处置通知。
4. **结果接收**：`POST /recall/notices/{no}/evidence` 接收三类结果——
   `platform_callback`（平台回调，按 `callback_id` 去重）、`manual_screenshot`（人工截图摘要）、
   `field_recheck`（现场复核），均声明已清理的展示位置。
5. **关闭判定**：任务关闭需同时满足——无未结升级；至少两类结果且来自不同提交人
   （重复回调只留档不计入，不能提前关闭）；结果覆盖全部展示位置；
   若事件要求替换，替代素材已登记且放行凭证有效。全部任务关闭后事件自动关闭。

## 四条升级路径

| 升级 | 触发 | 入口 |
| --- | --- | --- |
| `unreachable` 无法联系 | 签收期限已过未确认 | `POST /recall/cases/{case}/sweep` 扫描或手工 escalate |
| `refused` 拒绝下架 | 渠道明确拒绝 | `POST /recall/notices/{no}/refuse` |
| `partial_replacement` 只替换部分页面 | 宣称完成但结果未覆盖全部位置 | 提交 `final=true` 结果时自动进入 |
| `reappeared` 旧图再次出现 | 监测发现已撤回内容指纹 | `POST /recall/sightings`，自动重开任务与事件 |

升级未结（`POST /recall/escalations/{ref}/resolve`）前任务不得关闭。

## 替代素材放行

替代素材必须引用 `POST /recall/credentials` 由独立审核人签发的放行凭证：
凭证须有效、未过期、与素材匹配，且签发人不得为事件风险负责人本人。
旧素材已撤回这一事实不构成任何放行依据。

## 追溯与监测

- `GET /recall/cases/{case}/trace`：从最初风险通知到每个渠道实际下架/替换时间的完整时间线。
- `GET /recall/dashboard/coverage`：管理层覆盖率、逾期任务数、未结升级分布。
- `POST /recall/sightings`：持续上报再传播线索，命中已撤回指纹即自动重开并升级。
