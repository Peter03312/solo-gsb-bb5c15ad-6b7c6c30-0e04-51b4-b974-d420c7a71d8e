# 批次追溯服务（batch-trace）

面向小型食品加工厂的**本地批次追溯服务**：登记原料批次、记录生产投料与产出、
查询批次余量与上下游谱系，并在发现疑似污染批次时找出并隔离全部后代产出。

- 语言/框架：Python 3.12 + FastAPI + SQLAlchemy 2
- 关系数据库：PostgreSQL 16（Docker Compose 一键启动）；无数据库时自动回退本地 SQLite 文件库
- 计量：`NUMERIC(18,4)` 定点数，数量必须为**正数**，同一笔单据内**单位一致**
- 部署：Dockerfile 构建镜像，`docker compose up` 安装依赖、建库建表并启动

---

## 一、快速启动

### 方式 A：Docker Compose（推荐，含 PostgreSQL）

```bash
docker compose up --build -d
```

启动后：

- API 服务地址：`http://localhost:8080`
- 健康检查：<http://localhost:8080/health>
- 交互式接口文档（Swagger UI）：<http://localhost:8080/docs>

API 容器会等待 PostgreSQL 健康后自动**建库建表**（`app/entrypoint.py` 调用 `CREATE TABLE IF NOT EXISTS ...`），
数据持久化在 compose 卷 `trace_pgdata` 中。

停止与清理：

```bash
docker compose down          # 停止，保留数据
docker compose down -v       # 停止并删除数据库卷
```

### 方式 B：本地直接运行（自动使用 SQLite）

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.entrypoint
```

不设置 `DATABASE_URL` 时会在项目根目录创建 `trace.db`，服务同样监听 `http://0.0.0.0:8080`。

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

---

## 二、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | 见下 | SQLAlchemy 数据库连接串。compose 中为 `postgresql+psycopg2://trace:trace@db:5432/trace`；未设置时回退 `sqlite:///./trace.db` |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `db` / `5432` / `trace` / `trace` / `trace` | 未设置 `DATABASE_URL` 时用于拼装 PostgreSQL 连接串 |
| `HOST` | `0.0.0.0` | HTTP 监听地址 |
| `PORT` | `8080` | HTTP 监听端口 |
| `DB_POOL_SIZE` | `5` | PostgreSQL 连接池大小 |

compose 的端口映射：宿主机 `8080` → 容器 `8080`，可在 `docker-compose.yml` 的
`ports` 处修改宿主机端口。

---

## 三、HTTP 接口

所有写操作（`POST`）都必须携带请求头 `Idempotency-Key`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/raw-batches` | 登记一个原料批次及初始数量 |
| POST | `/productions` | 提交一次生产：多批次投入 → 一个产出批次 |
| GET | `/batches/{batch_no}` | 查询批次余量与状态 |
| GET | `/batches/{batch_no}/lineage` | 查询上下游谱系（递归，不含已撤销投料边） |
| POST | `/contamination` | 标记疑似污染批次，返回并隔离全部后代 |
| POST | `/reviews` | 质检复核确认误报：撤销疑似标记并重算隔离 |
| POST | `/productions/{output_batch_no}/revoke` | 凭产出批次编号 + 非空撤销原因撤销生产单 |
| GET | `/productions/{output_batch_no}` | 按产出编号查询生产单（原单据 + 撤销状态/原因） |

### 业务规则

1. 数量一律为正数，使用定点数计算；同一生产单内所有批次单位必须一致。
2. 产出量 **不得超过** 投入总量（允许加工损耗，即产出 < 投入）。
3. 批次编号全局唯一、**不得复用**（原料批次与产出批次共用编号空间）。
4. 来源不存在、用量无效（非正数）、单位不一致或余量不足时，**整笔拒绝，不改变任何余量**。
5. 污染规则：标记某批次疑似污染后，其全部直接/间接产出（混料后代）**整体隔离**；
   隔离批次（含疑似批次本身）**不得再投料**，提交生产时整笔拒绝。
6. 复核规则：质检复核确认误报时，`POST /reviews` 提交**当前疑似批次编号**与
   **复核说明**（非空白），撤销其疑似标记并重算该批次及全部后代的隔离状态——
   自身或任一祖先仍疑似则继续隔离，否则解除。混料后代不会因一个来源解除就忽略
   另一疑似来源；编号不存在报 `SOURCE_NOT_FOUND`(404)，批次当前非疑似（从未标记
   或已撤销）报 `BATCH_NOT_SUSPECT`(409)，二者均不改变任何状态。批次可经历
   “标记 → 复核撤销 → 再次标记 → 再次复核”。
7. 幂等：同一 `Idempotency-Key` + 同一请求体重试，返回首次的状态码与响应体、不重复扣减、不重复撤销；
   同一 key + 不同请求体返回 `409 IDEMPOTENCY_CONFLICT`。
8. 并发：生产提交在单事务内对来源批次加行锁（PostgreSQL `SELECT ... FOR UPDATE`，
   按编号排序加锁避免死锁；SQLite 以 `BEGIN IMMEDIATE` 串行化写事务），并发提交**不会超用**同一批次余量。
   污染标记/复核对“自身 + 全部后代”的闭包按编号排序加行锁，与生产事务互斥，
   并发结果等价于复核、污染标记、投料三者的某一串行顺序；隔离批次绝不会被投料。
9. 生产单撤销（录入错误更正）：操作员凭**产出批次编号**与**非空白撤销原因**调用
   `POST /productions/{output_batch_no}/revoke`。仅允许撤销同时满足以下条件的产出，
   任一不符整笔拒绝、**不改变任何库存**：
   - 产出编号存在且确为某生产单的产出（否则 `PRODUCTION_NOT_FOUND` 404）；
   - 该生产单**尚未撤销**（重复撤销报 `PRODUCTION_ALREADY_REVOKED` 409）；
   - 产出批次**当前非疑似**（`OUTPUT_IS_SUSPECT` 409）；
   - 产出**无复核记录**（`OUTPUT_HAS_REVIEW` 409）；
   - **从未被后续有效生产投料**（被投料报 `OUTPUT_ALREADY_CONSUMED` 409；
     投料它的后续单若也已撤销，则该投料边已失效，不构成阻碍）。

   撤销在**同一事务**内把各来源的**原投料量逐笔返还**（来源批次即使已隔离也照样返还；
   隔离只限制再投料，不限制返还），产出批次余量清零并标记为已撤销。
   撤销是**软删除**：产出编号、原生产单（含投料明细）与撤销原因永久保留，
   可经 `GET /productions/{output_batch_no}` 与批次查询检索；编号**不得复用**。
   撤销后：已撤销产出**不得再投料**（`BATCH_REVOKED` 409）；该单投料边**不再进入
   有效谱系查询，也不再参与疑似隔离传播**（撤销前已被传播打上的隔离标志不影响该规则；
   撤销不会改变任何批次的疑似/隔离状态，因而不会误放行仍疑似的来源）。
   撤销事务与投料、污染标记、复核共用同一套串行化锁，并发结果等价于某一串行顺序，
   不会超用余量或误放行。撤销写请求同样要求 `Idempotency-Key`，沿用同体重放、
   异体 `409 IDEMPOTENCY_CONFLICT` 约定。`GET /batches/{batch_no}` 响应新增布尔字段
   `is_revoked`（默认 `false`，旧调用方无需改动）；其余既有接口字段与未撤销数据的
   行为完全保持兼容。

### 错误响应格式

```json
{ "error": { "code": "INSUFFICIENT_BALANCE", "message": "批次 'RM-1' 余量不足：请求 200，剩余 100", "details": { } } }
```

常见错误码：`SOURCE_NOT_FOUND`(404)、`PRODUCTION_NOT_FOUND`(404)、`BATCH_NO_REUSED`(409)、
`INSUFFICIENT_BALANCE`(409)、`UNIT_MISMATCH`(422)、`OUTPUT_EXCEEDS_INPUT`(422)、
`VALIDATION_ERROR`(422)、`BATCH_QUARANTINED`(409)、`BATCH_NOT_SUSPECT`(409)、
`IDEMPOTENCY_CONFLICT`(409)，以及撤销相关的 `BATCH_REVOKED`(409)、
`PRODUCTION_ALREADY_REVOKED`(409)、`OUTPUT_IS_SUSPECT`(409)、
`OUTPUT_HAS_REVIEW`(409)、`OUTPUT_ALREADY_CONSUMED`(409)。

---

## 四、HTTP 调用样例

### 1. 登记原料批次

```bash
curl -X POST http://localhost:8080/raw-batches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: raw-rm-a-001' \
  -d '{"batch_no":"RM-A","unit":"kg","quantity":"100","note":"高筋面粉"}'
```

```json
{"batch_no":"RM-A","unit":"kg","initial_qty":100.0,"remaining_qty":100.0,
 "is_suspect":false,"is_quarantined":false,"note":"高筋面粉"}
```

### 2. 提交一次混料生产（多批次投入 → 一个产出）

```bash
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-mix1-001' \
  -d '{
        "output_batch_no": "MIX-1",
        "unit": "kg",
        "output_quantity": 90,
        "inputs": [
          {"batch_no": "RM-A", "quantity": 40},
          {"batch_no": "RM-B", "quantity": 55}
        ]
      }'
```

### 3. 安全重试（同一 key + 同一内容）

```bash
# 与上一条完全相同的请求：返回原结果，余量只扣一次
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-mix1-001' \
  -d '{ ...同上... }'

# 同一 key 换不同内容：
# 409 {"error":{"code":"IDEMPOTENCY_CONFLICT", ...}}
```

### 4. 查询批次余量

```bash
curl http://localhost:8080/batches/RM-A
# {"batch_no":"RM-A","unit":"kg","initial_qty":100.0,"remaining_qty":60.0, ...}
```

### 5. 查询上下游谱系

```bash
curl http://localhost:8080/batches/RM-A/lineage
```

```json
{
  "batch_no": "RM-A",
  "upstream": [],
  "downstream": [
    {"batch_no": "MIX-1", "depth": 1, "remaining_qty": 40.0,
     "unit": "kg", "is_suspect": false, "is_quarantined": false},
    {"batch_no": "FG-1",  "depth": 2, "remaining_qty": 50.0,
     "unit": "kg", "is_suspect": false, "is_quarantined": false}
  ],
  "edges": [
    {"from": "RM-A", "to": "MIX-1"},
    {"from": "MIX-1", "to": "FG-1"}
  ]
}
```

`depth` 表示与查询批次相隔的生产层数；`edges` 给出当前谱系子图中的“原料 → 产出”有向边。

### 6. 标记疑似污染并找出全部后代

```bash
curl -X POST http://localhost:8080/contamination \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cont-20260930-01' \
  -d '{"batch_no":"RM-A","note":"抽检农残超标"}'
```

```json
{
  "source": "RM-A",
  "is_suspect": true,
  "affected_batch_nos": ["MIX-1", "FG-1"],
  "newly_quarantined": ["MIX-1", "FG-1"]
}
```

`MIX-1`（混料时混入 RM-A）与 `FG-1`（间接后代）被**整体隔离**；与 RM-A 无关的批次不受影响。
此后再用隔离批次投料会被拒绝：

```bash
curl -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-fg2-001' \
  -d '{"output_batch_no":"FG-2","unit":"kg","output_quantity":5,
       "inputs":[{"batch_no":"MIX-1","quantity":5}]}'
# 409 {"error":{"code":"BATCH_QUARANTINED","message":"批次 'MIX-1' 已隔离，隔离批次不得再投料"}}
```

### 7. 质检复核：确认误报，撤销疑似标记并重算隔离

沿用上例：`RM-A` 已被标记疑似，混料后代 `MIX-1 = RM-A + RM-B`、`MIX-1 -> FG-1`
均被隔离。质检复检确认 RM-A 为误报：

```bash
curl -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: review-rm-a-20260930-01' \
  -d '{"batch_no":"RM-A","review_note":"留样复测未检出农残，确认误报"}'
```

```json
{
  "reviewed_batch_no": "RM-A",
  "is_suspect": false,
  "review_note": "留样复测未检出农残，确认误报",
  "affected_batch_nos": ["MIX-1", "FG-1"],
  "released_batch_nos": ["FG-1", "MIX-1", "RM-A"],
  "still_quarantined": []
}
```

- `affected_batch_nos`：本次重算涉及的全部后代；
- `released_batch_nos`：本次**解除隔离**的批次（复核批次自身 + 不再有任何疑似
  祖先的后代）；`still_quarantined`：重算范围内仍须隔离的批次。
- 此后 `GET /batches/{no}` 与 `/batches/{no}/lineage` 同步反映 `is_suspect=false`、
  `is_quarantined=false`；解除隔离的批次可重新投料。

混料来源不会被“连带误放”：若 RM-B 当时也处于疑似状态，撤销 RM-A 只会放行
RM-A 自身，`MIX-1`/`FG-1` 因仍有疑似祖先 RM-B 而继续隔离：

```json
{ "released_batch_nos": ["RM-A"], "still_quarantined": ["FG-1", "MIX-1"] }
```

非法复核明确报错且不改变状态：

```bash
# 编号不存在
curl -i -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: rv-x' \
  -d '{"batch_no":"GHOST","review_note":"x"}'
# 404 SOURCE_NOT_FOUND

# 已撤销 / 从未疑似
curl -i -X POST http://localhost:8080/reviews \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: rv-a2' \
  -d '{"batch_no":"RM-A","review_note":"再次撤销"}'
# 409 BATCH_NOT_SUSPECT
```

### 8. 整笔拒绝示例（余量不足时不产生副作用）

```bash
# RM-A 当前余量 60，却请求消耗 200
curl -i -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: prod-bad-001' \
  -d '{"output_batch_no":"MIX-X","unit":"kg","output_quantity":200,
       "inputs":[{"batch_no":"RM-A","quantity":200}]}'
# HTTP/1.1 409 ... INSUFFICIENT_BALANCE；MIX-X 未创建，RM-A 余量仍为 60
```

### 9. 撤销录入错误的生产单

沿用第 2 节的混料生产（`RM-A 40 + RM-B 55 -> MIX-1 90`，投料后 RM-A 余 60、RM-B 余 25）。
发现录单错误后，凭**产出批次编号**与**非空撤销原因**撤销：

```bash
curl -X POST http://localhost:8080/productions/MIX-1/revoke \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: revoke-mix1-001' \
  -d '{"output_batch_no":"MIX-1","revoke_reason":"投料量多录 10kg，撤销后重新开单"}'
```

```json
{
  "output_batch_no": "MIX-1",
  "status": "REVOKED",
  "revoke_reason": "投料量多录 10kg，撤销后重新开单",
  "revoked_at": "2026-09-30T03:12:44.812033+00:00",
  "returns": [
    {"batch_no": "RM-A", "quantity": 40.0},
    {"batch_no": "RM-B", "quantity": 55.0}
  ],
  "output_quantity": 90.0,
  "unit": "kg"
}
```

- `returns` 为各来源的**原投料量返还明细**：RM-A 余量回到 100、RM-B 回到 80；
  **即使来源批次当时已隔离，也在同一事务内照额返还**（隔离只禁止再投料）。
- 产出 `MIX-1` 余量清零并标记 `is_revoked=true`；编号保留、不得复用。

```bash
curl http://localhost:8080/batches/MIX-1
# {"batch_no":"MIX-1","unit":"kg","initial_qty":90.0,"remaining_qty":0.0,
#  "is_suspect":false,"is_quarantined":false,"note":null,"is_revoked":true}
```

原生产单与撤销原因留痕，可随时按产出编号查询：

```bash
curl http://localhost:8080/productions/MIX-1
```

```json
{
  "output_batch_no": "MIX-1", "unit": "kg", "output_quantity": 90.0,
  "inputs": [
    {"batch_no": "RM-A", "quantity": 40.0},
    {"batch_no": "RM-B", "quantity": 55.0}
  ],
  "status": "REVOKED",
  "revoke_reason": "投料量多录 10kg，撤销后重新开单",
  "revoked_at": "2026-09-30T03:12:44.812033+00:00"
}
```

撤销后的约束：

```bash
# 1) 已撤销产出不得投料
curl -i -X POST http://localhost:8080/productions \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: prod-fg2-002' \
  -d '{"output_batch_no":"FG-2","unit":"kg","output_quantity":5,
       "inputs":[{"batch_no":"MIX-1","quantity":5}]}'
# 409 BATCH_REVOKED

# 2) 撤销边退出有效谱系：RM-A 的下游不再含 MIX-1
curl http://localhost:8080/batches/RM-A/lineage
# {"batch_no":"RM-A","upstream":[],"downstream":[],"edges":[]}

# 3) 撤销边不再传播污染：标记 RM-A 时 MIX-1 不受影响
# （affected_batch_nos 不含 MIX-1，MIX-1 的 is_quarantined 仍为 false）

# 4) 重复撤销明确拒绝，且不二次返还
curl -i -X POST http://localhost:8080/productions/MIX-1/revoke \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: revoke-mix1-002' \
  -d '{"output_batch_no":"MIX-1","revoke_reason":"再撤一次"}'
# 409 PRODUCTION_ALREADY_REVOKED；RM-A/RM-B 余量不变

# 5) 其它条件不符同样整笔拒绝、不改库存：
#    编号不存在/编号非产出 -> 404 PRODUCTION_NOT_FOUND
#    当前疑似             -> 409 OUTPUT_IS_SUSPECT
#    有复核记录           -> 409 OUTPUT_HAS_REVIEW
#    已被后续生产投料     -> 409 OUTPUT_ALREADY_CONSUMED
#    撤销原因为空白       -> 422 VALIDATION_ERROR；缺少 Idempotency-Key -> 400
```

安全重试与其它写接口完全一致：同一 `Idempotency-Key` + 同一请求体重试返回首次结果、
只返还一次；同键异体返回 `409 IDEMPOTENCY_CONFLICT`。

---

## 五、项目结构

```
app/
  config.py       # 环境变量配置
  db.py           # 引擎/会话、SQLite 写锁与 PG 行锁支撑、建表
  models.py       # batches / productions / production_inputs / batch_reviews /
                  # production_revocations / idempotency_keys
  schemas.py      # Pydantic 请求响应模型（正数、定点数校验）
  errors.py       # 业务错误码
  service.py      # 核心逻辑：登记、生产事务、撤销事务、幂等、递归谱系、污染传播、复核重算
  routes.py       # HTTP 路由
  main.py         # FastAPI 应用与异常处理
  entrypoint.py   # 容器入口：等待数据库 → 建表 → 启动
tests/            # 端到端 + 并发测试（58 个用例）
Dockerfile
docker-compose.yml
```

## 六、并发与一致性说明

- 每次生产提交是一个数据库事务：先按编号升序逐个锁定全部来源批次
  （全局统一加锁顺序，杜绝生产事务间死锁），再依次校验单位、隔离状态、
  余量与产出总量，全部通过后才扣减并写入产出；任一校验失败立即回滚。
- PostgreSQL 依赖 `SELECT ... FOR UPDATE` 行锁串行化同一批次的扣减；
  SQLite 写事务以 `BEGIN IMMEDIATE` 开始并设置 `busy_timeout`，写操作全局串行。
- 污染标记、复核与生产撤销是低频 QC/单据维护操作：事务一开始先取同一把串行化锁
  （PostgreSQL 为 `LOCK TABLE batches IN SHARE ROW EXCLUSIVE`，与生产事务的
  行锁/写入互斥；SQLite 由 `BEGIN IMMEDIATE` 全库写锁覆盖），随后读取/写入闭包或
  返还余量。因此
  - 复核 / 污染标记 / 投料 / 撤销任意并发，结果都等价于某一串行顺序；
  - 隔离批次不可能被投料，已撤销产出也不可能被并发投料（拿不到与
    QC/撤销事务的串行化位置，校验到的标志与最终谱系一致）；
  - 撤销在表锁内再按编号升序锁定各来源并返还，与生产提交同一全局加锁顺序，
    返还与扣减互不覆盖、绝不超用；撤销不会修改任何批次的疑似/隔离标志，故不会误放行；
  - 复核重算隔离状态时以“自身疑似或任一祖先疑似”为谓词，混料后代只要还有
    一个疑似来源就继续隔离。
- 谱系/隔离的递归边统一经“有效边”视图：存在撤销记录的生产单其投料边被
  `NOT EXISTS production_revocations` 过滤，撤销边既不进入谱系查询，也不传播隔离。
- 幂等记录与业务数据在同一事务内落库，唯一约束 `(method, path, key)` 兜底并发首请求竞争。

> 注：新表 `batch_reviews`、`production_revocations` 随启动/入口的
> `CREATE TABLE IF NOT EXISTS` 自动创建，既有数据库升级无需手工迁移；现有登记、
> 生产、污染标记、复核、批次/谱系查询接口的请求、响应与启动方式均保持不变
> （批次查询仅新增默认 `false` 的 `is_revoked` 字段）。
