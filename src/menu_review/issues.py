"""收件问题分类。

每个问题码都携带一句给审核员的**处置指引**，明确区分两种结果：

* ``RESUBMIT`` —— 申报方材料问题，补齐/更正后重新提交即可；
* ``ESCALATE`` —— 超出当前系统能力（如未来版本），需要升级系统。

问题分两个层级：

* :data:`SEVERITY_REJECT` —— 阻断收件（rejected / pending_upgrade）；
* :data:`SEVERITY_WARNING` —— 不阻断，但必须在结果中向审核员展示
  （如声明自身重复键：系统按标准覆盖语义取最后一个值，审核员应知晓）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 处置动作
RESUBMIT = "RESUBMIT"
ESCALATE = "ESCALATE"
NOTE = "NOTE"

# 问题层级
SEVERITY_REJECT = "reject"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    """一次收件中发现的单个问题。"""

    code: str
    message: str
    action: str
    severity: str = SEVERITY_REJECT

    def describe(self) -> str:
        """供审核员界面展示的中文描述，含明确处置指引。"""
        if self.severity == SEVERITY_WARNING:
            return f"[{self.code}] {self.message}（提示：不阻断收件，审核员知悉即可）"
        guidance = "请退回申报方补正材料后重新提交" if self.action == RESUBMIT else "请升级系统后再处理"
        return f"[{self.code}] {self.message}（处置：{guidance}）"


# ---- 结构性问题（输入不是完整的 JSON 对象） -----------------------------

NOT_JSON = Issue(
    "NOT_JSON",
    "输入不是合法的 JSON 文本",
    RESUBMIT,
)
TRUNCATED_JSON = Issue(
    "TRUNCATED_JSON",
    "JSON 内容不完整，文件可能在传输中被截断",
    RESUBMIT,
)
NOT_OBJECT = Issue(
    "NOT_OBJECT",
    "JSON 顶层不是对象（应为花括号包裹的声明）",
    RESUBMIT,
)
EMPTY_INPUT = Issue(
    "EMPTY_INPUT",
    "文件为空，没有任何声明内容",
    RESUBMIT,
)
TRAILING_CONTENT = Issue(
    "TRAILING_CONTENT",
    "JSON 对象之后存在多余尾随内容，无法确认是否夹带了其它声明",
    RESUBMIT,
)

# ---- 内容问题（对象完整，但字段不合规） ---------------------------------

DUPLICATE_KEY = Issue(
    "DUPLICATE_KEY",
    "声明中存在重复键，系统只能取最后一个值，存在数据歧义",
    RESUBMIT,
)
UNKNOWN_FIELD = Issue(
    "UNKNOWN_FIELD",
    "出现当前合同未定义的字段，无法确认其含义",
    RESUBMIT,
)
TIME_NO_TIMEZONE = Issue(
    "TIME_NO_TIMEZONE",
    "occurred_at 缺少时区信息，无法判定事件发生的准确时刻",
    RESUBMIT,
)
MISSING_IDENTIFIER = Issue(
    "MISSING_IDENTIFIER",
    "缺少 record_id 申报标识，无法登记与去重",
    RESUBMIT,
)
ILLEGAL_REVISION = Issue(
    "ILLEGAL_REVISION",
    "revision 非法：必须是不小于 0 的整数（负数修订号不允许进入归档）",
    RESUBMIT,
)

# ---- 版本问题 -----------------------------------------------------------

FUTURE_VERSION = Issue(
    "FUTURE_VERSION",
    "声明来自更新的合同版本，当前系统无法解读",
    ESCALATE,
)
UNKNOWN_VERSION = Issue(
    "UNKNOWN_VERSION",
    "schema_version 缺失或不是整数，无法判断适用的合同版本",
    RESUBMIT,
)
UNSUPPORTED_VERSION = Issue(
    "UNSUPPORTED_VERSION",
    "声明使用了已停止支持、且无迁移路径的旧版本",
    ESCALATE,
)
