"""结构化问题与动作提示：让审核员知道该补材料还是升级系统。"""

from __future__ import annotations

from dataclasses import dataclass

# 动作提示
ACTION_FIX_SUBMISSION = "fix_submission"  # 补正材料后重新提交
ACTION_UPGRADE_SYSTEM = "upgrade_system"  # 需要升级系统
ACTION_REVIEW = "review"  # 进入人工复核
ACTION_NONE = "none"  # 无需操作

# 结构阶段：输入是不是一个完整的 JSON 对象
EMPTY_INPUT = "empty_input"
INVALID_JSON = "invalid_json"
NOT_JSON_OBJECT = "not_json_object"
TRAILING_CONTENT = "trailing_content"
DUPLICATE_KEY = "duplicate_key"

# 字段阶段：对象内部逐项校验
UNKNOWN_FIELD = "unknown_field"
MISSING_FIELD = "missing_field"
MISSING_IDENTIFIER = "missing_identifier"
INVALID_VALUE = "invalid_value"
INVALID_TIMESTAMP = "invalid_timestamp"
MISSING_TIMEZONE = "missing_timezone"
INVALID_REVISION = "invalid_revision"
INVALID_SCHEMA_VERSION = "invalid_schema_version"
FUTURE_SCHEMA_VERSION = "future_schema_version"

# 运行期
INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class Issue:
    """单条问题：机器可读的代码 + 面向审核员的说明 + 建议动作。"""

    code: str
    message: str
    field: str | None = None
    action: str = ACTION_FIX_SUBMISSION


class DeclarationError(ValueError):
    """单份声明读取失败，携带全部结构化问题。"""

    def __init__(self, issues):
        self.issues = tuple(issues)
        super().__init__("；".join(issue.message for issue in self.issues) or "声明无效")
