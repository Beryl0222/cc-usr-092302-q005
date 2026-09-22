# 多语菜单安全译审

入境旅游平台让菜品翻译、食材和过敏原说明经过来源可查的审定。

`fixtures/menu_claim.json` 保存一条经过脱敏的业务样例，源代码只定义读取这份样例所需的最小合同。后续模块应保持既有标识和时间含义，新增状态必须说明迁移方式。

## 菜单声明正式收件

在最小合同之上，`menu_review` 现在提供可用于正式收件的读取管线
（`menu_review.ingest.IngestionService`）：

1. **先判定完整性**：识别空文件、语法错误、被截断的 JSON、顶层不是对象、
   对象后的尾随内容、对象内重复键（结构栈平衡 + 重复键 hook，均不抛异常）。
2. **再逐类报告内容问题**，每条结论带明确处置指引——
   `RESUBMIT`（退回申报方补材料）或 `ESCALATE`（升级系统）：
   - `TRAILING_CONTENT` 尾随内容
   - `DUPLICATE_KEY` 重复键
   - `UNKNOWN_FIELD` 未知字段（供应商备注事故：不再在对象构造阶段抛异常）
   - `TIME_NO_TIMEZONE` 无时区时间
   - `MISSING_IDENTIFIER` 缺失 record_id
   - `ILLEGAL_REVISION` 修订号缺失/非整数/为负（负修订号不再进入归档）
3. **版本策略**：
   - v0 旧声明经 `migrate_v0_to_v1` **显式迁移**为 v1（补齐
     `source=legacy-v0-migration`，标识与时间含义不变），迁移以 warning
     形式留痕；
   - 更高版本一律不解读，原文逐字节封存入等待升级区（`PEND-xxxx`），
     处置指引为升级系统，同原文只存一份。
4. **批量收件**：结果严格按拖入顺序返回，单份坏文件不牵连其它文件。
5. **只增不改的归档**：
   - 完全相同的再次上传 → 直接返回既有归档号，不重复归档、不重复通知；
   - 同 `record_id` 内容变化 → 开启复核单（`REV-xxxx`），历史归档保持不动；
   - 同一变更再次上传 → 沿用已有复核单。
6. **停机恢复**：归档与待发通知写入同一 outbox；通知通道故障后，
   `recover_pending_actions()` 只补做未完成动作，按归档号/复核单号幂等，
   已送达的通知绝不重发，恢复任务可安全重复执行。

审核员界面可直接渲染 `SubmissionResult.describe()` 的中文结论。

## 本地检查

运行 `python -m unittest discover -s tests`。

- `tests/test_contracts.py`：既有合同与业务样例（`load_record` 用法不变）；
- `tests/test_ingest.py`：各类输入、版本迁移、未来版本保全、批量保序、
  去重/复核幂等与停机恢复的最终状态。
