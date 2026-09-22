# 食品品牌宣传合规台 · 多渠道撤回与替换后端

当某组喉糖宣传可能让消费者误认为含药材或具备药品功效时，素材往往已被直营网店、
经销商短视频账号、线下屏幕和明星团队多次转载。**发出一封下架邮件不能证明市场上已经停止传播。**
本服务把"原始素材 → 裁剪/改字派生件 → 投放账号 → 渠道负责人 → 可核验展示位置"
组成传播清单，逐点位发出带唯一编号的处置通知，受理平台回调 / 人工截图 / 现场复核作为结果，
经核验后才闭环，并持续发现已撤回内容的再次传播。

## 能力

- 传播清单：素材（含派生件关系与 sha256 指纹）、账号、渠道负责人、可核验点位
- 风险负责人圈定撤回范围、动作（下架/替换）与期限；按渠道负责人分组发出唯一编号通知
- 三类结果凭证 + 联系结果；平台回调按投递编号幂等，**重复回调不推进任何状态**
- 核验制闭环：声称完成 ≠ 完成；全部点位闭环通知才完成
- 四条独立升级路径：无法联系、拒绝下架、旧图再现、只替换部分页面（1→2→3 级，可重发通知）
- 替代素材须引用合规独立审核的放行凭证（批次、有效期窗口）；旧素材撤回不自动合规
- 数据范围：经销商仅本点位；代言团队不可见配方与消费者材料；管理层实时覆盖率与逾期视图
- 持续监测：指纹命中已撤回内容自动复发升级，已结束事件自动重新激活
- 全链路追溯：`/trace` 从最初风险通知到每个点位实际下架/替换时间

## HTTP 接口（摘要）

| 方法 | 路径 | 角色 |
| --- | --- | --- |
| POST | `/v1/incidents` | risk_owner |
| POST | `/v1/incidents/{ref}/manifest` | risk_owner, legal |
| GET | `/v1/incidents/{ref}/placements` | 按数据范围过滤 |
| POST | `/v1/incidents/{ref}/scope` | risk_owner |
| POST | `/v1/incidents/{ref}/notices` | risk_owner, legal |
| POST | `/v1/incidents/{ref}/evidence` | 品牌侧全量 / 经销商·代言限本点位截图复核 |
| POST | `/v1/evidence/{ref}/verify` | risk_owner, legal |
| POST | `/v1/assets/{ref}/release` | compliance |
| GET/POST | `/v1/escalations[/{ref}/advance|/resolve]` | risk_owner, legal |
| POST | `/v1/observations` | system, field_auditor, risk_owner, legal |
| POST | `/v1/scan/overdue` · GET `/v1/overdue` | 品牌侧 |
| GET | `/v1/dashboard/coverage` | executive, risk_owner, legal |
| GET | `/v1/incidents/{ref}/trace` | 按数据范围过滤 |

完整字段见 `contracts/entities.json`，请求/响应模型见 `app/models.py`，
可交互文档在服务启动后访问 `/docs`。

## 本地开发

运行 `make migrate` 初始化数据文件，`make test` 执行自动化检查，`make run` 启动服务。
也可以使用 `docker compose up --build` 构建并运行容器，宿主机端口通过 `APP_PORT` 调整。

演示令牌（`X-API-Token`，无真实身份信息）：

| 角色 | 令牌 |
| --- | --- |
| 风险负责人 | `tok-risk-1` |
| 品牌法务 | `tok-legal-1` |
| 合规审核员 | `tok-comp-1` |
| 现场复核员 | `tok-audit-1` |
| 管理层 | `tok-exec-1` |
| 东区/西区经销商 | `tok-dealer-east` / `tok-dealer-west` |
| 代言团队 | `tok-talent-1` |
| 监测系统 | `tok-system` |

服务通过 HTTP 接口交换业务事件，并使用 SQLite 文件保存本地状态。监听端口由 `PORT`
指定，数据文件位置由 `DATABASE_PATH` 指定；`contracts/entities.json` 记录稳定字段，
`fixtures/example.json` 提供不含真实身份信息的示例。
