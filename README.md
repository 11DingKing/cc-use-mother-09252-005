# 产教融合实训岗位承诺

面向业务人员的纯服务端系统：把企业季度承诺、院校需求、学生资格与安全培训有效期
纳入**同一版本账本**，按"不可拆分岗位 + 地区公平"生成分配建议；企业缩减承诺时
先保护已确认学生，再对候补重新编排并保留影响说明。

## 分层结构

```
service_09252_005/
├── domain/            # 纯业务规则：季度/跨期资格、不可拆分、地区公平匹配
├── persistence/       # SQLAlchemy ORM：版本链、部分唯一索引、补偿任务、追加账本
├── services/          # 用例编排：导入(幂等)、匹配、确认、缩减补偿、替补、结算
├── api/               # FastAPI：路由、令牌分级鉴权、PII 脱敏
├── clock.py           # 可替换时间端口（FixedClock 供测试复现）
└── config.py          # 数据目录/连接串（运行数据不写入源码目录）
```

## 核心规则

- **岗位不可拆分**：一个岗位整体分配给一所院校（`ux_active_allocation` 唯一索引）。
- **地区公平**：匹配按轮次进行，每轮每个仍有缺口的地区至多取一个岗位
  （满足率最低的地区优先），地区内剩余需求比例最高的院校优先；
  返回结果含 `fairness_score`，并提供分地区承诺缺口报告。
- **跨期资格**：安全培训必须同时在核验日有效 **且覆盖到季度结束日**
  （`有效期 >= quarter_end`）；季度中途过期判定为 `expires_during_quarter`。
- **并发不重复占座**：`ux_student_active_seat` 部分唯一索引保证一个学生
  同一季度只能处于 `proposed/confirmed` 一个席位；确认使用 `with_for_update`
  行锁 + savepoint，并发重复确认被串行化、重复导入被批次键幂等拦截。
- **缩减补偿**：已确认学生一律保护（即使岗位撤销或名额砍到已确认人数以下，
  状态置 `revoked_protected` 并写影响说明）；被挤出的拟分配学生优先重排到
  本校其他空缺席位，否则按序进入候补并统一提升。补偿以任务落库，
  进程重启时自动续跑未完成任务，也可由 `POST /compensations/run` 手动触发。
- **审计**：承诺版本链不可变；所有状态变化追加写入 `ledger_events`。

## API 概览（均需 Bearer 令牌）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/bootstrap-token` | 首启创建管理员（仅一次） |
| POST | `/admin/tokens` | 管理员签发 admin/enterprise/school 令牌 |
| POST | `/commitments` | 导入/更新企业承诺（`batch_key` 幂等，自动形成版本） |
| POST | `/commitments/reduce` | 缩减承诺并触发补偿编排 |
| GET  | `/commitments/{id}` | 查看承诺版本链与岗位 |
| POST | `/demands` | 导入院校需求（幂等批次） |
| POST | `/students/import` | 导入学生与安全培训有效期（幂等批次） |
| GET  | `/students/{id}` | 学生档案（按角色 PII 脱敏） |
| GET  | `/students/{id}/eligibility?quarter=&position_id=` | 跨期资格核验 |
| POST | `/match/{quarter}` | 生成/重新生成匹配建议（管理员） |
| GET  | `/match/{quarter}/allocations` | 分配建议/确认状态/影响说明 |
| GET  | `/match/{quarter}/waitlist` | 候补名单 |
| GET  | `/match/{quarter}/gap` | 分地区承诺缺口 |
| POST | `/allocations/{id}/confirm` | 院校确认（确认时复核资格） |
| POST | `/allocations/{id}/substitute` | 学生退出，候补按 rank 自动替补 |
| POST | `/quarters/{quarter}/settle` | 季度结算（幂等） |
| POST | `/compensations/run` | 手动续跑未完成补偿（重启后自动执行） |
| GET  | `/compensations` | 补偿任务状态 |
| GET  | `/ledger` | 追加式账本事件查询 |

权限分级：`admin` 全权；`enterprise` 只能维护本企业承诺，学生 PII 脱敏；
`school` 只能维护本校需求/学生、确认本校分配。

## 运行

```bash
pip install -r requirements.txt
export ADMIN_TOKEN=dev-admin-token        # 可选：启动时自动建好管理员
python3 -m service_09252_005              # 默认 127.0.0.1:8080
```

数据库默认使用系统临时目录下的 SQLite；可用 `TRAINING_DB_URL` 指向其它
SQLAlchemy 后端（部分唯一索引同时兼容 SQLite 与 PostgreSQL）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：不可拆分/地区公平轮转、跨期（季度内过期/跨季度）资格、并发确认与
并发占座唯一索引、缩减时已确认保护与候补重编排、重启后续跑补偿、
重复导入幂等不占名额、结算幂等、角色隐私权限。

## 编译检查

```bash
python3 -m compileall -q service_09252_005 tests
```
