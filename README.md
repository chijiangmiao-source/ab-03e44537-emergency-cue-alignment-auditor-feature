# 应急插播计划比对服务

Python 3.13 + FastAPI 纯后端。接收含 `planned`、`actual` 两数组的 JSON，用加权序列差异匹配对齐应急插播计划与实播日志，找出漏播、多播与漂移，并给出确定性的首个违规位置。

## 代价模型

| 操作 | 条件 | 代价 |
| --- | --- | --- |
| `MATCH` | 两项 `code` 相同且时间差绝对值 ≤ **2000 ms** | 时间差绝对值（ms） |
| `DELETE` | 计划项无对应实播（漏播） | **2500** |
| `INSERT` | 实播项无对应计划（多播） | **2500** |

对齐使**总成本最小**。总成本并列时，按**完整操作串字典序**裁决，操作顺序固定为 `MATCH` < `DELETE` < `INSERT`。操作串与路径一一对应，因此重复 `code` 也只有一条最优路径，同一请求逐次调用结果逐字节一致。

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

请求体（JSON 对象，仅 `planned`、`actual` 两数组参与校验，顶层多余字段被忽略）：

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

`details` 按确定顺序（结构错误 → 数组项错误 → 单调性错误）携带稳定机器码：

| 机器码 | 含义 |
| --- | --- |
| `INVALID_PAYLOAD` | 请求体不是合法 JSON 或不是 JSON 对象 |
| `MISSING_FIELD` | 缺少 `planned` / `actual` / `code` / `at_ms` |
| `INVALID_TYPE` | 数组或数组项类型错误 |
| `UNEXPECTED_FIELD` | 数组项含 `code`、`at_ms` 以外的字段 |
| `INVALID_CODE` | `code` 不是匹配 `[A-Z0-9]{1,16}` 的字符串 |
| `INVALID_AT_MS` | `at_ms` 不是非负整数（含布尔、浮点、字符串、负数） |
| `NOT_STRICTLY_INCREASING` | 数组时间未严格递增 |

同一非法请求逐次返回的响应体逐字节一致。

## 本地开发

```bash
pip install -r requirements-dev.txt
pytest                      # 穷举小序列对照暴力枚举 + 平局/边界/422 用例
uvicorn app.main:app --port 8000
```

测试以暴力枚举全部对齐路径为独立判据，穷举长度为 0–4、时间网格含恰好 500/2000 ms 边界的全部序列对（81×81），另有种子化随机模糊用例与显式平局用例。

## Docker

```bash
docker compose up api                      # 默认映射 8000 端口
API_PORT=9000 docker compose up api        # 由 API_PORT 覆盖宿主机端口
docker compose up --exit-code-from verify  # 一次性验收：14 项检查，退出码即结果
```

`verify` 服务依赖 `api` 健康检查后启动，对运行中的 API 执行合规、500/2000 ms 边界、重复码平局、多条等成本路径、逐次一致性与 422 机器码等验收，全部通过则以 0 退出。算法复杂度为 O(n·m) 时间与空间（n、m 为两数组长度）。
