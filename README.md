# 本地 SaaS 用量计量与阶梯计费引擎

一个**纯本地**的用量计量与计费引擎：所有客户、计量事件、价格计划、账期和账单
只存储在**本地 JSON 文件或进程内存**中。不连接 Stripe、数据库、支付网关、
云计费服务或任何外部服务；仅使用 Python 标准库。

## 金额表示

所有金额与用量均使用 `decimal.Decimal`(BigDecimal 等价物），**禁止 float**。
入账金额统一按最小货币单位（分，`0.01`）以 `ROUND_HALF_UP` 舍入。
单价可以是小数（如 `0.0001`/次），数量也可以是小数（如 `1.5` GB)。

## 计费公式

### 三种价格模型

设某客户在某账期内某指标的**计费用量**为 `Q`（已扣免费额度）。

- **flat（固定单价）**:`amount = Q × unit_price`
- **volume tier（总量阶梯）**：找到 `Q` 落入的唯一阶梯，**全部** `Q` 按该阶梯
  单价计费：`amount = Q × price(tier(Q))`
- **graduated tier（分段阶梯）**：每个阶梯只对本区间内的用量计费并求和：
  `amount = Σ units_in_tier_i × price_i`

### 阶梯边界约定（两种阶梯一致）

阶梯 `i` 覆盖区间 **`(lower_i, up_to_i]`**（左开右闭）：

- 用量**恰好等于**边界 `up_to_i` 时，属于**较低**的那个阶梯；
- 最后一个阶梯的 `up_to` 为 `None`，表示 `+∞`；
- 边界必须严格递增。

例：graduated 阶梯 `(0,100]→0.10`、`(100,1000]→0.05`、`(1000,∞)→0.01`：

| Q    | 计算                            | 金额    |
|------|---------------------------------|---------|
| 100  | 100×0.10                        | 10.00   |
| 101  | 100×0.10 + 1×0.05               | 10.05   |
| 1000 | 100×0.10 + 900×0.05             | 55.00   |
| 1001 | 100×0.10 + 900×0.05 + 1×0.01    | 55.01   |

volume 阶梯 `(0,100]→0.10`、`(100,∞)→0.05`:`Q=100` → `10.00`;`Q=101` →
全部 101 按 0.05 计 → `5.05`。

### 免费额度 / 封顶 / 最低消费的执行顺序

每个指标、每个账期严格按以下固定顺序执行：

1. **聚合**：汇总该账期内该指标所有事件的 `quantity` → 毛用量 `gross`；
2. **免费额度**:`billable = max(0, gross − free_allowance)`;
3. **计价**：按 flat / volume / graduated 模型对 `billable` 计算 → 金额；
4. **封顶（cap，指标级）**:`line = min(amount, cap)`;
5. **汇总**：各指标行求和 → 账单小计 `subtotal`;
6. **最低消费（minimum_spend，账单级）**:`total = max(subtotal, minimum_spend)`。

## 账期规则

- 账期为**月度**周期，半开区间 `[start, end)`;
- 由两个**固定可配置**参数决定：`PeriodRule(timezone, anchor_day)`，
  例如 `PeriodRule("Asia/Shanghai", anchor_day=1)`;
- **绝不使用机器默认时区**：所有事件时间戳先转换到配置的 IANA 时区再归期；
- `anchor_day` 为每月账期起始日（1–31)，短月自动钳制（如 2 月 anchor=31 → 28 日）;
- 时间戳必须带时区信息，否则拒绝。

## 幂等策略

- 每个用量事件携带全局唯一 `event_id`;
- 相同 `event_id` 且内容相同 → 返回 `duplicate`,**不重复计费**;
- 相同 `event_id` 但内容不同 → 返回 `conflict`，保留首次记录；
- 事件落库（内存或 JSON 文件）后才返回 `accepted`。

## 迟到事件（late events)

- 账期通过 `close_period(customer, period_key)` 关闭，关闭时账单被**快照冻结**,
  之后任何重算都返回该快照，绝不会静默改变；
- 迟到事件（其原始账期已关闭）**不会**写入已关闭账单，而是作为
  **adjustment（调整项）** 计入其后第一个未关闭的账期， ingest 返回
  `late_adjusted` 状态（含原账期与目标账期）;
- 调整项在目标账期账单中单独列出（事件 id、指标、数量、原始账期），其用量
  并入目标账期的聚合，按同一价格计划计费。

## 确定性

对同一输入事件集合，无论输入顺序如何，最终账单完全一致：聚合为可交换的
Decimal 求和，事件按 `event_id` 排序处理，指标行按名称排序输出。

## 存储

- `InMemoryStore`：纯进程内存；
- `JsonFileStore(path)`：单文件 JSON 持久化，每次变更原子写盘（tmp + rename)。

## 运行测试

```bash
cd <项目根目录>
python3 -m unittest discover -s tests -v
```

共 18 个测试，覆盖：阶梯边界（volume/graduated)、免费额度、封顶、最低消费、
规则执行顺序、重复事件、冲突事件、迟到事件 adjustment、多期关闭跳过、
输入乱序一致性、大数量（10¹²)、小数价格与小数用量、固定时区归期、
anchor 日与短月钳制。每个测试都会在终端打印其生成的账单明细。

## 运行演示

```bash
python3 -m billing.cli                # 纯内存
python3 -m billing.cli state.json     # 本地 JSON 文件持久化
```

演示包含：三种价格模型、免费额度、封顶、最低消费、重复事件去重、
账期关闭、迟到事件 adjustment，并打印各期账单明细。

## 代码结构

```
billing/
  money.py     # Decimal 金额工具（禁止 float）
  models.py    # UsageEvent / Tier / PricePlan / Invoice 等数据模型
  pricing.py   # flat / volume / graduated 计价器
  periods.py   # 固定时区 + anchor 日的账期规则
  storage.py   # 内存 / 本地 JSON 文件存储
  engine.py    # 幂等摄入、账期聚合、账单生成与冻结
  cli.py       # 本地演示
tests/
  test_billing.py
```
