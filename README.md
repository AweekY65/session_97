# 本地 SaaS 用量计量与阶梯计费引擎

纯本地实现：所有客户、计量事件、价格计划、账期与账单只存储在**本地 JSON 文件或内存**中。
不连接 Stripe、数据库、支付网关、云计费服务或任何外部服务。

## 金额表示

所有金额与用量均使用 `decimal.Decimal`（与 BigDecimal 等价的十进制任意精度语义），
**禁止 float**（`billing/money.py` 会拒绝 float 输入）。金额在账单行与账单合计处
以 `ROUND_HALF_UP` 一次性量化到 2 位小数（最小货币单位，如分）。

## 计费公式

每个 metric、每个账期一条账单行，规则**严格按以下顺序**执行：

1. **聚合**：累加账期内该 metric 的全部用量（正常事件 + 迟到调整事件）。
2. **免费额度**：`billable = max(0, total_quantity - free_allowance)`（按量扣除）。
3. **计价**（三种模型）：
   - `flat`：`amount = billable × unit_price`
   - `volume`：总量落入某一档，**全部用量**按该档单价计费：`amount = billable × tier.unit_price`
   - `graduated`：逐档累进，每档内的用量按该档单价计费后求和。
4. **最低消费**：若 `amount < minimum`，提升至 `minimum`。
5. **封顶**：若 `amount > cap`，压低至 `cap`（最后执行，优先于最低消费）。

**阶梯边界**：档位由含上界 `up_to` 定义（`None` 表示无界），区间为左开右闭
`(up_to[i-1], up_to[i]]`。例如档位 100 / 500：用量恰好 100 全部落在第一档；
100.5 时 100 个按第一档、0.5 个按第二档计费。

## 账期规则

- 账期为**日历月**，锚定日 `anchor_day`（默认 1 号）与 IANA 时区（默认 `UTC`）
  通过 `PeriodRule(tz_name, anchor_day)` 固定配置，**绝不使用机器默认时区**。
- 账期边界为半开区间 `[start, end)`：恰好落在结束边界上的事件属于下一期。
- 事件时间戳必须带显式 UTC 偏移（如 `2026-01-31T16:00:00+00:00`），
  先换算到配置时区再归入账期；不带偏移的时间戳直接报错。

## 幂等策略

- 以 `event_id` 为幂等键：同一 id 重复提交返回 `duplicate`，**绝不重复计费**
  （即使 payload 不同）。已处理 id 持久化在本地存储中，重启后仍有效。
- **确定性**：聚合为可交换的求和，账单输出（账单行、调整项）均按规范顺序排序，
  同一事件集合无论以何种顺序输入，最终账单完全一致（有测试保证）。

## 迟到事件

- 账期通过 `close_period_containing(ts)` 关闭，关闭时生成账单快照，**之后不可变**。
- 迟到事件（时间戳落在已关闭账期）不会静默修改旧账单，而是作为 **adjustment**
  计入 `now` 所在的当前开放账期，返回状态 `adjusted`，并在账单 `adjustments`
  中记录原始账期。
- 若未提供 `now` 或当前账期也已关闭，返回 `rejected` 及明确原因，事件不计费。

## 本地存储

- `MemoryStore`：纯内存。
- `FileStore(dir)`：本地 JSON 文件（`events.jsonl` 追加日志 + `state.json`
  记录已处理事件 id、已关闭账期与账单快照），无任何外部依赖。

## 目录结构

```
billing/
  money.py    # Decimal 金额/数量运算，ROUND_HALF_UP 量化
  models.py   # UsageEvent / Tier / PricePlan / 三种计价模型
  periods.py  # 固定时区 + 锚定日的账期计算
  store.py    # 内存与本地 JSON 文件存储
  engine.py   # 幂等摄入、迟到调整、账期关闭、账单生成
tests/
  test_billing.py
```

## 测试

```bash
python3 -m pytest tests/ -s
```

`-s` 让每个测试在终端打印其断言的账单明细（JSON）。覆盖场景：
阶梯边界（volume / graduated）、免费额度、最低消费、封顶及其先后顺序、
重复事件、迟到事件（调整 / 拒绝）、输入顺序无关性、大数量（1e9）、
小数价格与小数用量、账期边界与时区、文件存储重启恢复。

## 快速示例

```python
from decimal import Decimal
from billing.engine import BillingEngine
from billing.models import PricePlan, Tier, UsageEvent
from billing.periods import PeriodRule
from billing.store import FileStore

plan = PricePlan(
    metric="api_calls", model="graduated",
    tiers=(Tier.of(100, "0.10"), Tier.of(500, "0.05"), Tier.of(None, "0.01")),
    free_allowance=Decimal(10), minimum=Decimal("1.00"), cap=Decimal("100.00"),
)
eng = BillingEngine([plan], PeriodRule("Asia/Shanghai", 1), FileStore("data"))
eng.ingest(UsageEvent.of("evt-1", "cust-1", "api_calls",
                         "2026-01-10T12:00:00+08:00", "250"))
eng.ingest(UsageEvent.of("evt-1", "cust-1", "api_calls",
                         "2026-01-10T12:00:00+08:00", "250"))  # duplicate，不计费
invoice = eng.invoice("cust-1", "2026-01-20T00:00:00+08:00")
```
