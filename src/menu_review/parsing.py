"""宽容的 JSON 对象解析。

标准库 ``json.loads`` 无法满足正式收件的两个要求：

1. 需要知道输入是否为**完整的 JSON 对象**——截断的文件不能与语法错误
   混为一谈（审核员看到的处置建议不同）；
2. 需要发现**重复键**——标准解析默认后者覆盖前者，等于静默丢数据。

本模块只负责"文本 → 完整对象 / 明确的解析问题"，不做业务校验
（字段、时间、修订号等在 :mod:`menu_review.validation` 中处理）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DuplicateKey:
    """同一段文本中重复出现的键（行号为 1 起始；0 表示未能定位）。"""

    key: str
    first_line: int
    last_line: int


@dataclass(frozen=True)
class ParsedDocument:
    """解析产物：要么持有完整对象，要么携带一个结构性解析问题。

    ``trailing`` 为 JSON 值之后的非空白尾随内容（按原样保留）；
    ``duplicates`` 按首次出现行号排列，同一键重复多次只报告一次。
    """

    ok: bool
    value: Any = None
    error: str | None = None
    trailing: str = ""
    duplicates: tuple[DuplicateKey, ...] = field(default_factory=tuple)
    raw: str = ""

    @property
    def is_complete_object(self) -> bool:
        """输入是否为一个完整的 JSON 对象。"""
        return self.ok and isinstance(self.value, dict)


class _DuplicateKeyRecorder:
    """``object_pairs_hook``：捕获**单个对象内**的重复键。

    hook 每次调用只收到一个对象的键值对，因此在单次调用内判重可以
    正确处理嵌套对象（不同层级的同名键不算重复）。
    """

    def __init__(self) -> None:
        self.duplicated: list[str] = []

    def __call__(self, pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        seen: set[str] = set()
        for key, value in pairs:
            if key in seen and key not in self.duplicated:
                self.duplicated.append(key)
            seen.add(key)
            result[key] = value
        return result


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _find_key_occurrences(text: str, key: str) -> tuple[int, int]:
    """在原始文本中定位某个键第一次与最后一次出现的行号。

    仅做轻量文本扫描：查找 ``"key"`` 的出现位置。JSON 字符串字面量中
    的转义引号形如 ``\\"``，扫描时跳过，避免误判。
    """
    needle = json.dumps(key, ensure_ascii=False)
    lines: list[int] = []
    pos = 0
    while True:
        idx = text.find(needle, pos)
        if idx < 0:
            break
        lines.append(_line_of(text, idx))
        pos = idx + len(needle)
    if not lines:
        return (0, 0)
    return (lines[0], lines[-1])


def parse_document(text: str) -> ParsedDocument:
    """把文本解析为 :class:`ParsedDocument`，绝不抛出异常。

    判定顺序：

    * 空文本 / 语法错误 / JSON 值被截断 → ``ok=False``，并给出错误类别；
    * JSON 值之后还有非空白内容 → 记录到 ``trailing``；
    * 对象内部出现重复键 → 记录到 ``duplicates``。
    """
    recorder = _DuplicateKeyRecorder()
    lead = len(text) - len(text.lstrip())
    try:
        value, end = json.JSONDecoder(object_pairs_hook=recorder).raw_decode(
            text[lead:]
        )
    except json.JSONDecodeError:
        stripped = text.strip()
        if not stripped:
            return ParsedDocument(ok=False, error="empty", raw=text)
        category = "truncated" if _has_unclosed_structure(stripped) else "syntax"
        return ParsedDocument(ok=False, error=category, raw=text)

    trailing = text[lead + end :]
    duplicates = [
        DuplicateKey(key, *_find_key_occurrences(text, key))
        for key in recorder.duplicated
    ]
    duplicates.sort(key=lambda item: (item.first_line, item.key))
    return ParsedDocument(
        ok=True,
        value=value,
        trailing=trailing.strip(),
        duplicates=tuple(duplicates),
        raw=text,
    )


def _has_unclosed_structure(text: str) -> bool:
    """扫描文本：结束时仍有未闭合的对象/数组或字符串，则视为被截断。

    纯括号平衡无法处理字符串内的括号与转义引号，因此跟踪字符串状态。
    例：``{"a": tru``（值被切断）→ 栈中仍有 ``{`` → 截断；
    ``{不是json}`` → 括号平衡 → 语法错误。
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if stack and {"{": "}", "[": "]"}[stack[-1]] == char:
                stack.pop()
    return in_string or bool(stack)
