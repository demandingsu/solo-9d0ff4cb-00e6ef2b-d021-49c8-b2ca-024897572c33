# 道路养护工时纠错服务

道路养护工时必须归到预算科目才能入仓。本服务用 FastAPI + SQLite 管理同月三张表，
判定每条工时的完整路径（工单 → 项目 → 预算科目），对重复工时提供事务化纠错。

## 一、数据模型（同月每表行号唯一）

| 表 | 含义 | 关键字段 | 主键 |
| --- | --- | --- | --- |
| `work_hour` | 工时表 | 工单号 `work_order`、分钟数 `minutes` | `(month, row_id)` |
| `dispatch` | 分派表 | 工单 → 项目 | `(month, row_id)` |
| `attribution` | 归科表 | 项目 → 预算科目 `budget_subject` | `(month, row_id)` |
| `correction_audit` | 纠错审计 | 撤除的表/行号、对应工时行 | 自增 id |

## 二、供数核算规则（集合式，不逐条展开路径）

完整路径 = 工时按工单号连分派、分派按项目连归科。核算由**一条集合式 GROUP BY SQL**
完成（见 `app/services.py` 的 `PATH_SQL`），服务端不按工时逐行 JOIN、不在 Python 追链：

- `path_count = 1`：**可供数**，返回工时及唯一预算科目；
- `path_count = 0`：扣留，原因 `no_path`；
- `path_count >= 2`：扣留，原因 `duplicate`。**两条路径即使落到同一预算科目也算重复**
  （计数按 分派×归科 组合，而非按科目去重）。

## 三、纠错事务语义

`POST /months/{month}/corrections` 指定工时行号 + 要撤除的一条分派或归科记录，
在单个 `BEGIN IMMEDIATE` 事务内完成：

1. 以**撤除前已提交数据**为快照校验：工时行、被撤记录必须存在；被撤记录必须在该工时
   的某条完整路径上；目标工时当前必须为重复（路径数 ≥ 2）；
2. 撤除记录并写审计；
3. 撤除后重新集合核算，强制两条不变量：
   - 目标工时恰好剩 1 条路径（能供数）；
   - 原本可供数的工时全部保有科目（共享归科被误撤会在此被拦）；
4. 校验通过才 COMMIT，撤除与供数变化一起生效，返回 `restored_work_row_ids`
   （本次由扣留恢复为可供数的全部工时行号）；
5. 任一校验失败即 ROLLBACK，数据保持撤除前状态，响应体用中文说明原因
   （404 不存在 / 422 撤错记录 / 409 违反供数或保科规则）。

并发：所有写事务均为 `BEGIN IMMEDIATE` + WAL + `busy_timeout`，SQLite 写锁使并发纠错
在数据库层串行化，后到事务读到的必是前一个已提交版本，满足“以本次撤除前的已提交
数据为准”；锁冲突返回 409 提示基于最新数据重试。

## 四、月度镜像对账（只交换指纹与行号区间）

数仓与源端各持一份月度镜像，两端**不交换明细、只交换对账号、行号区间和指纹**：
先比对三表整表指纹，哪张表对不上就按对账号把对不上的段逐级查下一级，
直到单行叶子，即可区分**缺行**（叶子为空哨兵）与**内容不同**（叶子非空但指纹变了）。

- 生成：`POST /months/{month}/reconciliation` 返回对账号与工时/分派/归科三表整表指纹；
- 总览：`GET /reconciliation/{account_id}` 取三表 level 0 段；
- 下钻：`GET /reconciliation/{account_id}/tables/{table}/segments?lo=..&hi=..`
  返回该段下一级固定 32 个子段，空段 `is_empty=true` 并带固定空哨兵指纹；
- 叶子（段宽 1）不能再下钻（422），其指纹即单行指纹，是最终判定依据。

### 4.1 快照与原子语义

- 生成时录入照常提交：服务先在**一个短只读事务**里对三张表做一致性快照读，
  释放读锁后再用 `BEGIN IMMEDIATE` 一次性写入全部段指纹并登记对账号。
  因此三表所有指纹必然来自**同一个已提交快照**，不会出现跨快照的混合态；
- 任一步失败都不登记对账号、不残留任何段，**整轮不可查**（按对账号查返回 404）；
- 对账索引是只追加的独立表，源表清空（`DELETE /months/{month}`）或服务重启后，
  旧对账号仍可逐级查询，指纹保持不变。

### 4.2 对账索引存什么

只存「表名、行号范围、指纹」及定位所需的对账号/层级/空标记，**不存任何源表内容**：

```text
reconciliation_account(account_id PK, month, created_at)
reconciliation_segment(
    account_id, table_name, level, lo, hi, is_empty, fingerprint, created_at,
    PRIMARY KEY (account_id, table_name, level, lo, hi))
```

空段不落库——“空段标空，无须为每个空号建项”；查询时按固定网格在响应里补出空子段。
每表非空段数量 ≤ 行数 × 6，稀疏行号不会撑大索引。

## 五、两端照抄的分段与编码规则（确定性，与语言/框架无关）

指纹协议版本固定 `v1`。两端只要逐字实现以下规则，同表同段必得到相同指纹，
与录入顺序、行号疏密无关。`tests/run_recon_e2e.py` 内有一份仅用标准库的独立参考实现。

1. **行号宇宙固定为 `[1, 2^25)`，即 1..33,554,431**，与具体数据无关。
   超出上界的行号在生成时返回 422，不静默截断。
2. **分段树为固定 32 叉树，共 6 层（level 0..5）**，段宽只取决于层级：

   | level | 段宽 | 段数 | 含义 |
   | --- | --- | --- | --- |
   | 0 | 33,554,432 | 1 | 整表段（整表指纹） |
   | 1 | 1,048,576 | 32 | |
   | 2 | 32,768 | 1024 | |
   | 3 | 1,024 | 32768 | |
   | 4 | 32 | | |
   | 5 | 1 | | 叶子=单行 |

   通用公式：`WIDTH[L] = 2^(25 - 5L)`；行号 `r` 在 level L 所属段为
   `lo = floor((r-1)/WIDTH[L])*WIDTH[L] + 1`，`hi = lo + WIDTH[L] - 1`（闭区间）。
   因为边界由固定网格决定，**两端缺行、行号稀疏也会切出完全相同的区间**。
3. **单行规范编码**：UTF-8 的紧凑 JSON 数组（`separators=(",",":")`、
   `ensure_ascii=False`、列序固定，`row_id` 恒为首元素）：

   ```text
   work_hour   : [row_id, work_order, minutes]
   dispatch    : [row_id, work_order, project]
   attribution : [row_id, project, budget_subject]
   ```

   **行指纹 = `sha256(行编码)` 的小写十六进制**。
4. **段指纹**：
   - 空段（含空表整表段）：固定哨兵 `sha256(b"recon-empty-v1")` 的十六进制；
   - 叶子段（level 5，宽度 1）：**直接等于该行行指纹**；
   - 其余非空段（含 level 0 整表段）：把段内各行行指纹按十六进制字符串
     **字典序排序**后用 `|` 连接，前面加前缀 `recon-seg-v1|`，整体再做一次 sha256：
     `sha256(b"recon-seg-v1|" + b"|".join(sorted(行指纹)))`。
   哈希前排序保证录入顺序不影响结果；行号唯一且参与行编码，排序无歧义。
5. **空段判定**：响应中 `is_empty=true` 且指纹等于空哨兵即为空段，两端无需为空号建项。
6. 规则任何一项变化都应升协议版本，避免新旧两端误判为“内容不同”。

### 5.1 核对流程（两端各自执行）

```text
1. 两端分别 POST 生成对账号，交换账号与三表整表指纹；
2. 整表指纹相同 => 该表完全一致；不同则对该表从 level 0 开始：
   GET .../segments?lo=1&hi=33554432 取 32 个 level-1 子段，比对同区间指纹；
3. 仅对指纹不同的子段继续带它的 [lo,hi] 下钻，每层 32 选 1（或若干）；
4. 到 level 5 叶子：
   - 一端 is_empty=true、另一端非空        => 缺行/多行；
   - 两端都非空但行指纹不同                => 同行号内容不同；
   - 两端都空或指纹相同                    => 该叶子一致。
```

## 六、启动（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000/docs 即可录入、查询、纠错、对账
```

SQLite 文件落在命名卷 `maintenance-data:/data/maintenance.db`，容器重建数据不丢，
对账索引同样持久化在该卷内，重启后对账号继续可查。

## 七、接口一览（均在 /docs 可试）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/months/{month}/work-hours` | 录入工时表（`replace=true` 整月覆盖，`false` 追加，行号冲突 409） |
| POST | `/months/{month}/dispatches` | 录入分派表 |
| POST | `/months/{month}/attributions` | 录入归科表 |
| GET | `/months/{month}/supply` | 供数视图：可供数带科目，扣留带路径数与原因 |
| POST | `/months/{month}/corrections` | 纠错：撤除一条分派/归科 |
| GET | `/months/{month}/audit` | 纠错撤除审计 |
| POST | `/months/{month}/reconciliation` | 生成对账号，返回三表整表指纹（同一已提交快照） |
| GET | `/months/{month}/reconciliation` | 列出某月已生成、仍可查的对账号 |
| GET | `/reconciliation/{account_id}` | 按对账号取三表整表指纹（level 0） |
| GET | `/reconciliation/{account_id}/tables/{table}/segments` | 查某段下一级 32 个子段指纹（空段标空） |
| DELETE | `/months/{month}` | 测试辅助：清空某月业务数据（对账索引保留） |
| GET | `/health` | 健康检查（容器 HEALTHCHECK） |

`month` 格式固定 `YYYY-MM`。示例：

```bash
# 录入与纠错
curl -X POST localhost:8000/months/2026-11/work-hours -H 'Content-Type: application/json' \
  -d '{"rows":[{"row_id":1,"work_order":"WO1","minutes":120}]}'
curl localhost:8000/months/2026-11/supply
curl -X POST localhost:8000/months/2026-11/corrections -H 'Content-Type: application/json' \
  -d '{"work_row_id":1,"remove":"dispatch","remove_row_id":2}'

# 对账：生成 -> 整表 -> 下钻
curl -X POST localhost:8000/months/2026-11/reconciliation
curl localhost:8000/reconciliation/2026-11-<对账号后缀>
curl "localhost:8000/reconciliation/2026-11-<后缀>/tables/work_hour/segments?lo=1&hi=33554432"
```

## 八、实测结果

本机无 Docker 守护进程，使用与镜像一致的 Python 3.11 + 同款依赖实跑，
两套 e2e 均启动真实 uvicorn 进程、走真实 HTTP（测试仅依赖标准库）。

### 8.1 既有纠错链路（`tests/run_e2e.py`）

- **全量 e2e：38/38 断言通过**，覆盖同科目重复路径仍判重复、无路径 `no_path`、
  共享归科撤除 409 回滚并保科、撤除无关记录 422、对可供数工时撤除 409、
  正确撤除后恢复供数与审计落表、并发纠错串行化后数据自洽等；
- **8 路并发压力**：8 个独立重复工时同时纠错全部 200，恢复后科目逐个正确、审计 8 条。
- 本次新增对账能力后**原有用例零改动复跑仍全部通过**，无回归。

### 8.2 新增对账链路（`tests/run_recon_e2e.py`，独立参考实现交叉验证）

测试内置一份仅用标准库、按第五节规则独立实现的指纹算法，与服务端结果逐段比对：

- **录入顺序无关**：三表正序、逆序两次生成，整表指纹完全相同，且三表整表指纹与
  独立参考实现逐一相等；逐级下钻 5 层到叶子，叶子指纹等于独立实现的单行指纹；
- **漏传（缺行）**：少录一行后整表指纹变化，未改动的另外两表指纹不变；下钻到该叶子
  `is_empty=true`、指纹为空哨兵——可明确判定为缺行；
- **改值（内容不同）**：修改某行字段后该叶子仍非空、但行指纹变为改后值的参考指纹，
  与“缺行”形态明确区分；
- **稀疏行号**：使用行号 1、2、33554431 等极端稀疏数据，顶层 32 段中恰有 2 个非空、
  30 个空段且全部带空哨兵；直接查 SQLite 确认**空段零落库**，非空段数 ≤ 行数×6，
  全部已存段指纹与参考实现逐段一致；
- **源表清空 + 服务重启**：清空业务表并重启进程（同一个 SQLite 文件）后，旧对账号
  仍可查、整表与叶子指纹保持不变，对账号列表不丢；
- **生成时覆盖源表**：线程屏障让“生成对账”和“整月覆盖录入”同时发出，录入照常 200
  提交、对账也 200；工时整表指纹严格等于“覆盖前 3 行”或“覆盖后 1 行”两个已提交
  快照之一，绝不出现混合态，且对账号可重复查询；
- **失败整轮不可查**：通过环境变量 `RECON_FAULT=write` 在写事务中途注入故障，
  生成返回 500、对账号列表无新增、库中段/账号零残留；去掉故障开关重启后重试成功；
- **接口护栏**：不存在对账号 404；非对齐/越界区间 422；叶子段再下钻 422；
  空父段下钻返回 32 个全空子段；行号超过 2^25-1 时生成 422；非法月份 422。
- `/docs` 与 `/openapi.json` 返回 200，四个对账端点的请求/响应模型完整可见；
  `docker-compose.yml` YAML 解析通过，全部 Python 源 `py_compile` 通过。

## 九、余项

1. 当前环境无 Docker 守护进程，镜像未实际 `docker compose build`，仅做了
  compose/Dockerfile 静态校验；全部逻辑已用同版本依赖在本机完整试跑。
2. 行号宇宙固定为 `[1, 2^25)`（约 3355 万）。若未来单表行数需要更大空间，
   应升指纹协议版本并同步调整 `ROW_ID_BITS`，两端同步升级，不可静默改网格。
3. 对账索引只追加不清理；如需保留期治理，可按 `reconciliation_account.created_at`
   定期删除过期对账号及其段（先删段、再删账号），不影响业务表。
4. 故障注入开关 `RECON_FAULT` 仅用于测试，默认不设置；生产镜像不设置该变量。
5. 入参为 JSON 批量录入；如需 Excel/CSV 导入可在现有服务上层加解析，表结构无需改动。
6. 认证鉴权、分页未纳入本次范围；服务默认监听 8000，建议部署在内网或网关之后。
7. 测试依赖仅用标准库；若团队引入 pytest，可将两套 `tests/run_*_e2e.py` 的断言直接迁移。
