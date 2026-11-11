# 工时归属修订 API（FastAPI + SQLite）

财务数仓场景：员工调岗补录会改变成本中心归属，但**月结时的口径必须冻结**。
本服务以「递增修订号 + 只追加（append-only）存储」实现补录与历史口径并存：

- 人事导出的员工 / 成本中心 / 生效日期区间按批提交，每批成功返回一个**递增修订号**；
- 补录只覆盖同员工在指定区间内的归属，**区间外原归属保留，允许空档**；
- 日期区间为**左闭右开** `[effective_from, effective_to)`；
- 给定修订号查询工时归属时，只可见该修订号及之前的数据，**后续跨段补录不改变旧修订的查询结果**；
- 批内同员工区间重叠，或起日不早于止日时，**整批拒绝**：数据与修订号均不变。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与异常处理
│   ├── service.py     # 批次校验、修订提交、归属查询（核心业务规则）
│   ├── database.py    # SQLite 连接、WAL、建表
│   ├── schemas.py     # Pydantic 请求/响应模型
│   └── errors.py      # 业务错误类型
├── tests/test_api.py  # 端到端测试（pytest + TestClient）
├── scripts/demo.sh    # 命令行端到端演示
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
- SQLite 数据文件位于命名卷 `attribution-data`（容器内 `/data/attribution.db`），容器重建后修订历史保留。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据卷（清空所有修订）
```

## 二、通过交互式文档演示

打开 <http://localhost:8000/docs>，按以下顺序操作（各接口均含示例请求体，可直接 `Try it out`）：

1. **POST `/api/revisions`** 提交初始归属批次 → 返回 `revision_id: 1`
2. **POST `/api/attribution/query`** 用 `revision_id: 1` 查询工时 → 得到月结口径
3. 再次 POST `/api/revisions` 提交一条**非法批次**（起日 ≥ 止日，或同员工区间重叠）→ 返回 `400` 及错误码，且 `/api/revisions` 中看不到新修订号
4. POST `/api/revisions` 提交调岗补录（同员工、与旧区间跨批重叠的新段）→ 返回 `revision_id: 2`
5. 用 `revision_id: 1` 重放第 2 步的查询 → **结果与补录前完全一致**；再用 `revision_id: 2` 查询 → 补录区间内为新成本中心，区间外保留旧归属

也可以一条命令跑完等价的命令行演示：

```bash
./scripts/demo.sh
```

## 三、API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/revisions` | 提交一批归属区间，成功返回递增修订号（201） |
| GET | `/api/revisions` | 列出全部修订号 |
| POST | `/api/attribution/query` | 按指定修订号逐条返回工时成本中心 / 未归属 |
| POST | `/api/revisions/{id}/segments` | 查看某修订号提交的原始区间（审计） |

### 请求示例

提交修订：

```json
POST /api/revisions
{
  "note": "HR 调岗补录 2026-11",
  "entries": [
    {
      "employee_id": "E001",
      "cost_center": "CC-GZ-02",
      "effective_from": "2026-04-01",
      "effective_to": "2026-07-01"
    }
  ]
}
```

归属查询：

```json
POST /api/attribution/query
{
  "revision_id": 2,
  "records": [
    {"employee_id": "E001", "work_date": "2026-05-15", "hours": 8},
    {"employee_id": "E001", "work_date": "2026-03-15", "hours": 8}
  ]
}
```

响应：

```json
[
  {
    "employee_id": "E001", "work_date": "2026-05-15", "hours": 8.0,
    "revision_id": 2, "cost_center": "CC-GZ-02",
    "attributed": true, "matched_revision_id": 2
  },
  {
    "employee_id": "E001", "work_date": "2026-03-15", "hours": 8.0,
    "revision_id": 2, "cost_center": "CC-SH-01",
    "attributed": true, "matched_revision_id": 1
  }
]
```

未归属时 `cost_center` 为 `null`、`attributed` 为 `false`。`revision_id=0` 表示尚无任何补录的空口径。

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

   修订 N 提交后，对它可见的数据集合在物理上就固定了，后续修订号更大的数据天然不可见。
3. **整批原子性**。全部业务校验在写库前完成；修订号分配与区间插入在同一个
   `BEGIN IMMEDIATE` 事务内，失败即回滚，自增修订号不会被失败批次消耗。
4. **半开区间语义**。重叠判定 `a.from < b.to AND b.from < a.to`；首尾相接
   （`a.to = b.from`）不算重叠，`work_date = effective_to` 当天不归属该段。

## 五、本地开发与测试（不用 Docker）

```bash
pip install -r requirements-dev.txt
pytest -q
ATTRIBUTION_DB_PATH=./data/dev.db uvicorn app.main:app --reload
```

`ATTRIBUTION_DB_PATH` 可指定 SQLite 文件位置，默认 `./data/attribution.db`。

## 六、错误响应格式

```json
{
  "error": {
    "code": "OVERLAPPING_SEGMENTS",
    "message": "员工 E001 在本批存在重叠区间：第 1 条 [...] 与第 2 条 [...]",
    "details": {"employee_id": "E001", "indices": [0, 1]}
  }
}
```

| code | HTTP 状态码 | 触发条件 |
| --- | --- | --- |
| `INVALID_DATE_RANGE` | 400 | 起日不早于止日 |
| `OVERLAPPING_SEGMENTS` | 400 | 批内同一员工区间重叠 |
| `REVISION_NOT_FOUND` | 404 | 查询/审计了不存在的修订号 |
| Pydantic 校验错误 | 422 | 空批次、字段缺失、日期格式错误等 |
