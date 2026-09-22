# 多语菜单安全译审

入境旅游平台让菜品翻译、食材和过敏原说明经过来源可查的审定。

`fixtures/menu_claim.json` 保存一条经过脱敏的业务样例，源代码只定义读取这份样例所需的最小合同。后续模块应保持既有标识和时间含义，新增状态必须说明迁移方式。

## 声明读取

`read_declaration(text)` 分两个阶段读取单份声明，问题以结构化 `Issue` 列表逐项返回，每条都带动作提示（`fix_submission` 补正材料 / `upgrade_system` 升级系统）：

1. **结构阶段**——先识别输入是不是完整的 JSON 对象：空输入、语法错误、顶层非对象、重复键、尾随内容、缺失标识、非法 schema_version；
2. **字段阶段**——按声明自身的版本分别报告未知字段、缺失字段、无时区时间和非法修订号（revision 必须是正整数）。

`load_record(path)` 保持既有用法，无效输入抛出携带全部问题的 `DeclarationError`。

## 版本与迁移

- 当前合同为 `schema_version=1`。
- `schema_version=0` 的旧声明（时间字段为 `declared_at`、无 `revision`）仍受支持，经**显式迁移**生成当前记录：`declared_at` 更名为 `occurred_at`（时间含义不变），补 `revision=1`，标识不变。样例见 `fixtures/menu_claim_legacy_v0.json`。
- `schema_version` 大于当前版本的声明不做字段级解释，**原文原样保全**并进入 `pending_upgrade`（等待升级）状态。样例见 `fixtures/menu_claim_future_v2.json`。

## 收件与归档

`IntakeService.intake_batch` 按拖入顺序逐份处理，单份失败不牵连其余文件；`format_batch` 把每份结果按原顺序逐行呈现。归档语义：

- 完全相同（同记录号、同内容哈希）的再次上传直接返回既有归档号，不重复通知；
- 同记录号内容变化开启复核（`conflict_review`），候选版本登记在复核案件中，**不改写已归档历史**；
- 每份记录一个文件，通知状态与记录同文件原子写入。

## 故障恢复

归档与通知之间发生停机后，用同一目录重建 `ArchiveStore` 并调用 `IntakeService.recover()`：只补做未完成的通知（以通知 id 为幂等键，发送成功但标记落盘前停机也不会重复投递），不重归档、不重发已完成通知。

## 本地检查

运行 `python -m unittest discover -s tests`。
