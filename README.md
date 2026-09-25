# 产教融合实训岗位承诺

面向业务人员的纯服务端系统：把企业承诺、院校需求、学生资格与安全培训有效期
纳入同一版本账本，按不可拆分岗位与地区公平约束生成分配建议；企业缩减承诺时
先保护已确认学生，再对候补重新编排并保留影响说明。

代码按领域模型、应用服务、持久化与接口边界组织；时间、标识和外部输入通过
可替换端口接入（`ports.py`），以便稳定复现状态变化。运行数据与本地配置
不写入源码目录（默认数据库位于系统临时目录，可用 `--db` 或 `APP_DB_PATH` 指定）。

## 架构

```
service_09252_005/
  ports.py         可替换端口：Clock / IdGenerator
  domain.py        季度口径、分配状态、资格规则（安全培训覆盖整个季度）
  errors.py        领域错误 -> HTTP 状态映射
  store.py         SQLite 持久化：BEGIN IMMEDIATE 串行写事务、部分唯一索引
  ledger.py        单一版本账本（append-only，全局单调版本号）
  matching.py      纯函数匹配引擎：不可拆分岗位 + 最大最小地区公平 + 缺口报告
  compensation.py  缩减补偿执行器：protect -> replan -> finalize，断点续跑
  privacy.py       角色（admin/enterprise/school/student）与敏感字段脱敏
  services.py      应用服务：承诺/需求/学生/资格/匹配/确认/替补/结算/账本
  api.py           接口边界：路由 + 标准库 HTTP 服务
  app.py           组合根：装配并在启动时恢复未完成补偿
  __main__.py      python3 -m service_09252_005 --port 8080 --db /path/app.db
```

## 核心语义

- **版本账本**：承诺、需求、资格、分配的每次变化都追加到同一 `ledger_events`
  事件流，版本号全局单调；状态表只是投影。
- **不可拆分岗位**：每个名额整体授予一名学生；每名学生每季度至多一条生效分配
  （数据库部分唯一索引兜底）。
- **地区公平**：各地区确认名额不超过申报需求；容量不足时按最大最小公平分配，
  用不完的公平份额自动释放，避免名额浪费。
- **承诺缺口**：匹配后按地区、按技能输出需求-供给对照（`GET .../matching/{q}/gaps`），
  供院校尽早识别缺口。
- **缩减补偿**：容量下调创建补偿作业——已确认学生全部保留（溢出也保留并记
  `protected_overflow`），多余建议取消，空额按候补顺位递补；每一步独立事务并
  落检查点，进程重启后自动续跑，影响说明幂等不重复。
- **幂等导入**：导入接口接受 `idempotency_key`（查询参数或请求体字段）。同键同
  请求体重放返回首次结果；同键不同体重返 409；无键重复导入按自然键覆盖，
  均不会重复占用名额。
- **资格口径**：安全培训须在季度开始前完成且有效期覆盖整个季度；确认、递补、
  匹配各环节均复核。

## API 概览（/api/v1，需 X-Actor-Role / X-Actor-Id 请求头）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /enterprises、/schools | 注册企业/院校（admin） |
| POST | /commitments/import | 导入季度承诺（全量快照，幂等） |
| POST | /commitments/{id}/reduce | 缩减岗位容量，触发补偿 |
| GET  | /commitments/{id} | 承诺与岗位实时占用 |
| POST | /demands/import、/students/import | 导入需求/学生（幂等） |
| GET  | /students/{id} | 学生信息（按角色脱敏） |
| POST | /qualifications/verify | 批量资格核验 |
| POST | /matching/{quarter}/run | 生成分配建议与候补（admin） |
| GET  | /matching/{quarter}/gaps | 承诺缺口报告 |
| GET  | /allocations | 分配列表（按角色过滤与脱敏） |
| POST | /allocations/{id}/confirm | 确认（事务内复核容量与资格） |
| POST | /allocations/{id}/cancel | 取消 |
| POST | /allocations/{id}/substitute | 替补：取消并提拔候补（admin） |
| POST | /allocations/{id}/complete | 实训完成登记（企业/admin） |
| GET  | /compensations/{id} | 补偿作业状态与影响说明 |
| POST | /settlements/run、/settlements/{id}/finalize | 季度结算与终审（admin） |
| GET  | /settlements/{id} | 结算单（企业/admin） |
| GET  | /ledger/version、/ledger/events?since= | 版本账本（admin） |

## 运行

```bash
python3 -m service_09252_005 --host 127.0.0.1 --port 8080 --db /tmp/app.db
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：并发确认（容量原子性）、跨期资格（安全培训有效期）、隐私权限
（角色脱敏与越权拒绝）、幂等导入、缩减补偿与重启续跑、结算与替补、
匹配引擎公平性、HTTP 端到端冒烟。

## 编译检查

```bash
python3 -m compileall -q service_09252_005 tests
```
