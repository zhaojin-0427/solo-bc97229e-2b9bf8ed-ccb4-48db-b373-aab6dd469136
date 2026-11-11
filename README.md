# 工时归属修订与离线备份恢复 API（FastAPI + SQLite）

财务数仓场景：员工调岗补录会改变成本中心归属，但**月结时的口径必须冻结**。
本服务以「递增修订号 + 只追加（append-only）存储」实现补录与历史口径并存，
并提供**离线 SQLite backup 的两步式恢复 API**，供人事归属库从离线备份整体恢复、
工时入仓继续查询。

- 人事导出的员工 / 成本中心 / 生效日期区间按批提交，每批成功返回一个**递增修订号**；
- 补录只覆盖同员工在指定区间内的归属，**区间外原归属保留，允许空档**；
- 日期区间为**左闭右开** `[effective_from, effective_to)`；
- 给定修订号查询工时归属时，只可见该修订号及之前的数据，**后续跨段补录不改变旧修订的查询结果**；
- 批内同员工区间重叠，或起日不早于止日时，**整批拒绝**：数据与修订号均不变；
- **备份恢复**：上传 SQLite Online Backup 生成的两表备份，先暂存并做完整性、
  表结构、外键、每修订行数与区间规则校验；非法备份拒绝且不改现库，
  合法备份返回修订范围、记录数与一次性恢复凭据；确认时单事务原子替换，
  准备后若已有补录或其他恢复成功则拒绝，重复确认幂等返回原结果。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与异常处理（含备份恢复路由）
│   ├── service.py     # 批次校验、修订提交、归属查询（核心业务规则）
│   ├── recovery.py    # 备份暂存校验、原子切换、凭据与状态持久化、启动对账
│   ├── database.py    # SQLite 连接、WAL、显式事务/读快照、建表
│   ├── schemas.py     # Pydantic 请求/响应模型
│   └── errors.py      # 业务错误类型
├── tests/
│   ├── test_api.py       # 原有业务规则端到端测试
│   └── test_recovery.py  # 备份恢复端到端测试（校验/并发/崩溃/重启）
├── scripts/
│   ├── demo.sh           # 命令行端到端演示（补录与口径冻结）
│   ├── demo_recovery.sh  # 备份恢复端到端演示
│   └── make_backup.py    # 受信任侧备份生成/损坏样例工具（SQLite backup API）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```

## 一、Docker Compose 启动（交付方式）

```bash
docker compose up -d --build
```

- 服务地址：<http://localhost:8000>
- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
- ReDoc 文档：<http://localhost:8000/redoc>
- SQLite 数据文件位于命名卷 `attribution-data`（容器内 `/data/attribution.db`）；
  恢复暂存区与状态文件位于 `/data/recovery/`，容器重建后修订历史、
  **待确认备份与已完成恢复结果全部保留**。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据卷（清空修订与恢复状态）
```

## 二、通过交互式文档演示备份恢复（上传 → 确认 → 结果查询）

打开 <http://localhost:8000/docs>。先在容器外（或本机）用工具生成一份备份：

```bash
# 合法示例备份（含 2 个修订、3 条区间）
python scripts/make_backup.py --sample /tmp/backup.db
# 非法变体（任选其一，用于演示拒绝）：corrupt-header / integrity / extra-table /
# missing-fk / orphan-fk / row-count / revision-gap / bad-range / overlap
python scripts/make_backup.py --sample /tmp/bad.db --tamper orphan-fk
```

然后在 Swagger UI 中按顺序操作（接口均支持 `Try it out`，文件接口选择本地文件即可）：

1. **POST `/api/recovery/backups`**：`file` 选择 `/tmp/bad.db` 上传
   → 返回 `400` 与错误码（如 `BACKUP_FOREIGN_KEY_VIOLATION`），现库无任何变化；
2. 同一接口上传 `/tmp/backup.db`（`note` 可填）→ 返回 `201`，包含：
   - `min_revision_id` / `max_revision_id`（备份修订范围）、
     `revision_count` / `segment_count`（记录数）、`sha256`；
   - **`recovery_token`：一次性恢复凭据，仅此一次明文返回，请保存**；
3. （可选，演示冲突保护）先调 **POST `/api/revisions`** 补录一条，
   再调下面的确认接口 → 返回 `409 RECOVERY_STALE`，现库保持；
4. **POST `/api/recovery/backups/{backup_id}/confirm`**，请求体
   `{"recovery_token": "<上一步的 token>"}` → `200`，`status=confirmed`，
   返回恢复结果（恢复行数、修订范围、`next_revision_id = 备份最大号 + 1`）；
5. 再次发同样的确认请求 → **返回完全相同的原结果（幂等），不会再次恢复**；
6. **GET `/api/recovery/backups`** 或 **GET `/api/recovery/backups/{backup_id}`**
   查询暂存状态与恢复结果；重启服务后这两个接口仍可查到全部记录。

命令行等价演示：

```bash
./scripts/demo_recovery.sh     # 备份恢复全流程
./scripts/demo.sh              # 原有补录与口径冻结流程
```

## 三、API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/revisions` | 提交一批归属区间，成功返回递增修订号（201） |
| GET | `/api/revisions` | 列出全部修订号 |
| POST | `/api/attribution/query` | 按指定修订号逐条返回工时成本中心 / 未归属 |
| POST | `/api/revisions/{id}/segments` | 查看某修订号提交的原始区间（审计） |
| POST | `/api/recovery/backups` | 上传 SQLite backup 备份：暂存并校验，返回修订范围、记录数与恢复凭据（201） |
| POST | `/api/recovery/backups/{id}/confirm` | 凭恢复凭据确认恢复，原子整体替换（幂等） |
| GET | `/api/recovery/backups` | 列出全部暂存/已完成备份 |
| GET | `/api/recovery/backups/{id}` | 查询单个备份的暂存状态或恢复结果 |

### 备份上传响应示例

```json
{
  "backup_id": "bk-001a3b11faa04f599886a1538badcb9d",
  "status": "staged",
  "recovery_token": "dcfStwyAFmLCTTtuY-b4HgVTBAvY0cK_VjXeR5lSHTA",
  "filename": "backup.db",
  "note": "离线恢复批次 B1",
  "size_bytes": 24576,
  "sha256": "10ffd21c...",
  "revision_count": 2,
  "segment_count": 3,
  "min_revision_id": 1,
  "max_revision_id": 2,
  "prepared_against_max_revision": 0
}
```

### 确认恢复响应示例（节选）

```json
{
  "backup_id": "bk-001a...",
  "status": "confirmed",
  "confirmed_at": "2026-11-11T20:44:29.991Z",
  "recovery": {
    "restored_revision_count": 2,
    "restored_segment_count": 3,
    "restored_min_revision_id": 1,
    "restored_max_revision_id": 2,
    "next_revision_id": 3,
    "content_fingerprint": "ab871f23..."
  }
}
```

## 四、核心设计：旧口径为什么不会变

1. **只追加，不改写历史**。每批写入一条 `revisions` 记录与若干带 `revision_id` 的
   `attribution_segments`；任何修订都不执行 UPDATE/DELETE。
2. **查询带时间旅行条件**。修订 N 的查询只扫描 `revision_id <= N` 的区间，
   并对同一日期点取其中修订号最大者：

   ```sql
   WHERE employee_id = ?
     AND effective_from <= :work_date
     AND effective_to   > :work_date   -- 左闭右开
     AND revision_id <= :query_revision
   ORDER BY revision_id DESC, id DESC
   LIMIT 1
   ```
3. **整批原子性**。全部业务校验在写库前完成；修订号分配与区间插入在同一个
   `BEGIN IMMEDIATE` 事务内，失败即回滚，自增修订号不会被失败批次消耗。
4. **半开区间语义**。重叠判定 `a.from < b.to AND b.from < a.to`；首尾相接
   （`a.to = b.from`）不算重叠，`work_date = effective_to` 当天不归属该段。

## 五、备份恢复的关键设计

### 5.1 两步式流程与校验清单

1. **上传暂存（不改现库）**：文件先落到 `recovery/<backup_id>/backup.db`
   （临时文件 + `rename`，不会出现半截文件），随后以**只读 URI**
   （`mode=ro`、`trusted_schema=OFF`）打开并依次校验：
   - SQLite 文件头与 `PRAGMA integrity_check` 完整性；
   - 表结构：仅 `revisions`、`attribution_segments` 两张业务表
     （允许内部 `sqlite_sequence`），无额外表/视图/触发器；
     列定义、主键、`CHECK(effective_from < effective_to)`、
     指向 `revisions` 的外键声明、两个必需索引全部一致；
   - `PRAGMA foreign_key_check` 外键数据一致性（无孤儿区间）；
   - 每个修订号的区间行数与 `revisions.entry_count` 一致；
   - 修订号从 1 起**连续无缺口**，区间 id 正向唯一；
   - 区间规则：ISO 日期、起日严格早于止日、同修订同员工区间不重叠。

   任一失败即返回 400 并删除整个暂存目录，**现库连接都不会打开**；
   校验期间补录与查询正常服务。
2. **凭据确认（原子切换）**：见 5.2 ~ 5.4。

### 5.2 冲突保护：准备之后被改动则拒绝

暂存时记录现库全部业务行的 **SHA-256 内容指纹**与当前最大修订号。确认时重新计算指纹：

- 指纹变化 → 准备后发生过补录或另一次恢复成功 → `409 RECOVERY_STALE`，现库保持；
- 指纹一致 → 进入切换。补录提交与恢复切换由同一把进程内写锁互斥，
  「指纹校验 → 切换」之间不可能有写入穿插。

### 5.3 原子切换：崩溃后只有完整旧库或完整备份

切换在**单个 `BEGIN IMMEDIATE` 事务**内完成：

```text
ATTACH 暂存备份 AS backup;
DELETE FROM attribution_segments; DELETE FROM revisions;
INSERT ... SELECT ... FROM backup.<两张表>;
重置 sqlite_sequence（后续修订从备份最大号继续递增）;
提交前在同一事务内比对内容指纹与行数;
COMMIT;
```

事务原子性保证：提交前任何失败（含进程被 kill / 断电）都整体回滚，
磁盘上只可能是**完整旧库**或**完整备份**，不存在混合态。
切换前状态先持久化为 `switching`；启动时 `reconcile_on_startup()` 按
内容指纹对账——指纹等于备份则补记为 `confirmed`，否则退回 `staged` 允许重试。

### 5.4 并发查询不混合、确认幂等、状态持久

- **读快照隔离**：每次查询包在显式 `BEGIN ... COMMIT` 读事务里，WAL 快照
  固定到整次请求结束。切换提交的瞬间，进行中的查询要么全程读恢复前数据、
  要么在新快照上执行，**单次请求不会读到新旧混合的数据**。
- **幂等**：已 `confirmed` 的备份再次确认，凭据通过后直接返回原结果，
  不再执行任何写入；凭据错误一律 `403`。
- **持久化**：恢复状态写在 `recovery/index.json`（临时文件 + fsync + 原子
  rename），暂存备份、凭据哈希与已完成结果在重启后仍然可用。
- **凭据存储**：服务端只保存 `recovery_token` 的 SHA-256 哈希，
  明文 token 仅在上传响应中返回一次；比对使用恒定时间比较。

## 六、本地开发与测试（不用 Docker）

```bash
pip install -r requirements-dev.txt
pytest -q
ATTRIBUTION_DB_PATH=./data/dev.db uvicorn app.main:app --reload
```

`ATTRIBUTION_DB_PATH` 可指定 SQLite 文件位置，默认 `./data/attribution.db`；
恢复暂存目录固定为其同级的 `recovery/` 子目录。

## 七、错误响应格式

```json
{
  "error": {
    "code": "OVERLAPPING_SEGMENTS",
    "message": "员工 E001 在本批存在重叠区间：第 1 条 [...] 与第 2 条 [...]",
    "details": {"employee_id": "E001", "indices": [0, 1]}
  }
}
```

### 补录类错误

| code | HTTP | 触发条件 |
| --- | --- | --- |
| `INVALID_DATE_RANGE` | 400 | 起日不早于止日 |
| `OVERLAPPING_SEGMENTS` | 400 | 批内同一员工区间重叠 |
| `REVISION_NOT_FOUND` | 404 | 查询/审计了不存在的修订号 |
| Pydantic 校验错误 | 422 | 空批次、字段缺失、日期格式错误等 |

### 备份恢复类错误

| code | HTTP | 触发条件 |
| --- | --- | --- |
| `BACKUP_EMPTY` | 400 | 上传空文件 |
| `BACKUP_NOT_SQLITE` | 400 | 缺少 SQLite 文件头 |
| `BACKUP_INTEGRITY_FAILED` | 400 | `integrity_check` 失败、文件损坏或暂存文件被掉包 |
| `BACKUP_SCHEMA_MISMATCH` | 400 | 表/列/主键/CHECK/外键声明/索引不符，或含额外表、视图、触发器 |
| `BACKUP_FOREIGN_KEY_VIOLATION` | 400 | 外键检查失败、存在孤儿区间 |
| `BACKUP_ROW_COUNT_MISMATCH` | 400 | `entry_count` 与实际区间行数不一致 |
| `BACKUP_REVISION_GAP` | 400 | 修订号不从 1 起或存在缺口 |
| `BACKUP_INVALID_DATE_RANGE` | 400 | 日期格式错误或起日不早于止日 |
| `BACKUP_OVERLAPPING_SEGMENTS` | 400 | 同修订同员工区间重叠 |
| `BACKUP_TOO_LARGE` | 413 | 备份超过 50 MiB |
| `BACKUP_NOT_FOUND` | 404 | 备份 id 不存在或暂存文件丢失 |
| `RECOVERY_TOKEN_INVALID` | 403 | 恢复凭据错误 |
| `RECOVERY_STALE` | 409 | 准备后现库已被补录或被其他恢复修改 |
| `RECOVERY_CONFLICT` | 409 | 备份状态不允许确认 |
| `RECOVERY_SWITCH_FAILED` | 500 | 切换事务内自检失败（已回滚，现库不变） |
| `RECOVERY_STATE_CORRUPT` | 500 | 恢复状态文件损坏 |
