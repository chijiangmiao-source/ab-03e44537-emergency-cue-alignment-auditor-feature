# 应急插播计划比对服务

Python 3.13 + FastAPI 纯后端。接收含 `planned`、`actual` 两数组的 JSON，用加权序列差异匹配对齐应急插播计划与实播日志，找出漏播、多播与漂移，并给出确定性的首个违规位置。

## 代价模型

| 操作 | 条件 | 代价 |
| --- | --- | --- |
| `MATCH` | 两项 `code` 相同且时间差绝对值 ≤ **2000 ms** | 时间差绝对值（ms） |
| `DELETE` | 计划项无对应实播（漏播） | **2500** |
| `INSERT` | 实播项无对应计划（多播） | **2500** |

对齐使**总成本最小**。总成本并列时，按**完整操作串字典序**裁决，操作顺序固定为 `MATCH` < `DELETE` < `INSERT`。操作串与路径一一对应，因此重复 `code` 也只有一条最优路径，同一请求逐次调用结果逐字节一致。

## 候选路径（`alternative_limit`）

播控复核人员可在请求中加入可选整数 `alternative_limit`（**1 至 20**，非布尔整数）。启用后，除最优结果外响应追加 `alternatives`，按 `(总成本, 完整操作串)` 顺序给出至多 `alternative_limit` 条**合法完整路径**的前列候选（不含首选；合法路径不足时返回实际数量，可为空数组）。候选项字段：

| 字段 | 含义 |
| --- | --- |
| `total_cost` | 该候选路径总成本 |
| `cost_gap` | 相对首选路径的成本差（`total_cost - 首选总成本`，非负；并列裁决时为 0） |
| `pairs` | 该候选的完整配对（结构与顶层 `pairs` 一致） |
| `compliant` | 该候选自身的合规结论 |
| `first_defect` | 该候选自身从左到右的首个缺陷（合规时为 `null`） |
| `first_divergence_index` | 与首选路径**双方首次不同的操作索引**（首个操作即不同为 0） |

复核人员可据此判断首个缺陷是否依赖并列裁决：当 `cost_gap = 0` 且候选在缺陷位置之前分叉时，换一条等成本路径会改变缺陷结论。候选按全局统一顺序（总成本、完整操作串，`MATCH`/`DELETE`/`INSERT` 先后不变）生成，结果中不含重复路径。省略 `alternative_limit` 时请求解析、响应字段与序列化顺序与旧版逐字节一致（响应不含 `alternatives` 键）。

## 合规判定

仅当路径**全为 `MATCH`** 且每项漂移 **≤ 500 ms** 时 `compliant = true`。否则响应携带总成本、完整配对，以及路径中**从左到右首个缺陷**。

缺陷机器码（稳定）：

| 代码 | 含义 |
| --- | --- |
| `MISS` | 计划项被删除（漏播） |
| `EXTRA` | 实播项被插入（多播） |
| `DRIFT` | 匹配成功但漂移 > 500 ms |

## API

### `POST /align`

请求体（JSON 对象，`planned`、`actual` 两数组为必填，可选 `alternative_limit`，其余顶层字段被忽略）：

```json
{
  "planned": [{"code": "ADS1", "at_ms": 1000}],
  "actual": [{"code": "ADS1", "at_ms": 1200}]
}
```

每个数组项**仅含**两个字段：

- `code`：字符串，必须匹配 `[A-Z0-9]{1,16}`
- `at_ms`：非负整数（布尔、浮点、字符串均拒绝）

每个数组的 `at_ms` 必须**严格递增**，否则整体返回 422。

200 响应：

```json
{
  "compliant": true,
  "total_cost": 200,
  "pairs": [
    {
      "op": "MATCH",
      "cost": 200,
      "code": "ADS1",
      "planned_index": 0,
      "actual_index": 0,
      "planned_at_ms": 1000,
      "actual_at_ms": 1200,
      "drift_ms": 200
    }
  ],
  "first_defect": null
}
```

不合规时 `first_defect` 为路径中从左到右首个缺陷，例如：

```json
"first_defect": {
  "code": "MISS",
  "pair_index": 1,
  "pair": {"op": "DELETE", "cost": 2500, "code": "ADS1", "planned_index": 1, "planned_at_ms": 2000}
}
```

`pairs` 中 `DELETE` 项只带 `planned_*` 字段，`INSERT` 项只带 `actual_*` 字段，`MATCH` 项两者俱全并附 `drift_ms`。

需要候选路径时在请求体加入 `alternative_limit`（1–20 的非布尔整数；与 `planned`、`actual` 同为顶层字段）：

```json
{
  "planned": [{"code": "A", "at_ms": 0}, {"code": "A", "at_ms": 1000}],
  "actual": [{"code": "A", "at_ms": 500}],
  "alternative_limit": 1
}
```

响应在原有四字段之后追加 `alternatives`（示例为重复码等成本分叉，`cost_gap` 为 0、索引 0 处分叉）：

```json
"alternatives": [
  {
    "total_cost": 3000,
    "cost_gap": 0,
    "pairs": [
      {"op": "DELETE", "cost": 2500, "code": "A", "planned_index": 0, "planned_at_ms": 0},
      {"op": "MATCH", "cost": 500, "code": "A", "planned_index": 1, "actual_index": 0,
       "planned_at_ms": 1000, "actual_at_ms": 500, "drift_ms": 500}
    ],
    "compliant": false,
    "first_defect": {
      "code": "MISS",
      "pair_index": 0,
      "pair": {"op": "DELETE", "cost": 2500, "code": "A",
               "planned_index": 0, "planned_at_ms": 0}
    },
    "first_divergence_index": 0
  }
]
```

候选不足时 `alternatives` 为 `[]`（例如两空数组之间只有一条合法路径）。

### `GET /healthz`

返回 `{"status": "ok"}`，供容器健康检查使用。

## 422 校验错误

任何校验失败整体返回 422，不做部分对齐。响应格式稳定：

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "request body failed validation",
    "details": [
      {"code": "INVALID_CODE", "path": "planned[0].code", "message": "must be a string matching [A-Z0-9]{1,16}"}
    ]
  }
}
```

`details` 按确定顺序（结构错误：含两数组与 `alternative_limit` → 数组项错误 → 单调性错误）携带稳定机器码：

| 机器码 | 含义 |
| --- | --- |
| `INVALID_PAYLOAD` | 请求体不是合法 JSON 或不是 JSON 对象 |
| `MISSING_FIELD` | 缺少 `planned` / `actual` / `code` / `at_ms` |
| `INVALID_TYPE` | 数组或数组项类型错误 |
| `UNEXPECTED_FIELD` | 数组项含 `code`、`at_ms` 以外的字段 |
| `INVALID_CODE` | `code` 不是匹配 `[A-Z0-9]{1,16}` 的字符串 |
| `INVALID_AT_MS` | `at_ms` 不是非负整数（含布尔、浮点、字符串、负数） |
| `INVALID_ALTERNATIVE_LIMIT` | `alternative_limit` 不是 1–20 范围内的非布尔整数（含 `0`、`21`、负数、布尔、浮点、字符串、`null`、数组、对象） |
| `NOT_STRICTLY_INCREASING` | 数组时间未严格递增 |

同一非法请求逐次返回的响应体逐字节一致。

## 本地开发

```bash
pip install -r requirements-dev.txt
pytest                      # 穷举小序列对照暴力枚举 + 平局/边界/422 用例
uvicorn app.main:app --port 8000
```

测试以暴力枚举全部对齐路径为独立判据，穷举长度为 0–4、时间网格含恰好 500/2000 ms 边界的全部序列对（81×81），候选模式下另对 `alternative_limit` ∈ {1,3,20} 逐条核对前 `limit+1` 名与独立穷举器完全一致；另有种子化随机模糊用例、长序列裁剪压力用例与显式平局用例。

## Docker

```bash
docker compose up api                      # 默认映射 8000 端口
API_PORT=9000 docker compose up api        # 由 API_PORT 覆盖宿主机端口
docker compose up --exit-code-from verify  # 一次性验收：19 项检查，退出码即结果
```

`verify` 服务依赖 `api` 健康检查后启动，对运行中的 API 执行合规、500/2000 ms 边界、重复码平局、多条等成本路径、候选路径（唯一最优正成本差、重复码成本分叉、候选不足）、旧请求逐字节兼容、逐次一致性与 422 机器码等验收，全部通过则以 0 退出。算法复杂度为 O(n·m) 时间与空间（n、m 为两数组长度）；启用 `alternative_limit = L` 时为 O(n·m·(L+1)) 时间、O(n·m·(L+1)) 空间，DP 单元只保存 `(边代价, 后继坐标, 后继名次)`，不枚举路径、不复制完整操作串，仅对返回的至多 L 条候选做回溯物化。
