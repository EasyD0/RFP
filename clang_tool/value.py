"""
clang_tool.value - 从 AST 提取整型字面量 / 宏定义值.

- parse_int_literal: C 整型字面量文本 -> int
- literal_value_from_cursor: 整数 literal 游标 -> int
- get_macro_int_value: 简单对象宏 (如 #define N (3)) 的定义值 -> int
"""

import re
from pathlib import Path

from clang.cindex import Cursor, CursorKind, TranslationUnit
from MyPyLib.LogSet import logSetup

from .parse import parse_tu

logger = logSetup(__name__)


def parse_int_literal(text: str) -> int | None:
    """
    解析 C 整型字面量文本为数值.

    支持十进制、0x/0b/0o 前缀, 以及 u/U/l/L/i64 等后缀; 解析失败返回 None.
    注意 ``010`` 按 C 语义解析为八进制 8.
    """
    t = text.strip()
    # C 整型字面量必须以数字开头 (0x/0b/0o/十进制/八进制), 否则无法解析
    if not t or not t[0].isdigit():
        return None
    # 去掉整型后缀: 2u / 2UL / 2ll / 2i64 等
    t = re.sub(r"(?:[uUlL]+|i\d+)$", "", t)
    try:
        return int(t, 0)
    except ValueError:
        pass
    # C 八进制: 0 开头且只含 0-7 (Python 的 int("010", 0) 会拒绝前导零)
    if re.fullmatch(r"0[0-7]*", t):
        return int(t, 8)
    # 0 开头但含 8/9: C 中是非法的八进制字面量, 按无法解析处理
    if re.fullmatch(r"0[0-9]+", t):
        return None
    try:
        return int(t, 10)
    except ValueError:
        return None


def literal_value_from_cursor(cursor: Cursor) -> int | None:
    s, e = cursor.extent.start, cursor.extent.end
    if s.line != e.line or e.column <= s.column:
        return None
    try:
        with open(
            Path(str(cursor.location.file)), "r", encoding="utf-8", errors="ignore"
        ) as f:
            line_text = f.readlines()[s.line - 1]
    except Exception:
        return None
    # libclang 列按 UTF-8 字节计, 按字节区间切取再解码
    line_bytes = line_text.encode("utf-8", errors="ignore")
    return parse_int_literal(
        line_bytes[s.column - 1 : e.column - 1].decode("utf-8", errors="ignore")
    )


def get_macro_int_value(
    file_path, line_num: int, col_num: int, args: list | None = None
) -> int | None:
    """
    解析宏名位置处的简单对象宏的整型字面量值.

    仅支持 ``#define NAME <整型字面量>`` (可带括号, 如 ``#define N (3)``);
    复杂替换列表 (如 ``#define F 2+2``) 或解析失败返回 None.
    """
    tu = parse_tu(
        Path(file_path),
        args=args,
        options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
    )
    if tu is None:
        return None

    source_file = tu.get_file(str(file_path))
    if not source_file:
        return None

    location = tu.get_location(source_file.name, (line_num, col_num))
    cursor = Cursor.from_location(tu, location)
    if not cursor or cursor.kind != CursorKind.MACRO_INSTANTIATION:
        return None

    definition = cursor.get_definition()
    if not definition:
        return None
    try:
        tokens = list(definition.get_tokens())
    except Exception:
        return None
    if len(tokens) < 2:
        return None

    # 定义 tokens 形如 [宏名, 值...], 第一个是宏名
    value_tokens = [t.spelling for t in tokens[1:]]
    # 去掉外层括号: #define N (3)  →  tokens 为 ['N', '(', '3', ')']
    while len(value_tokens) >= 2 and value_tokens[0] == "(" and value_tokens[-1] == ")":
        value_tokens = value_tokens[1:-1]
    if len(value_tokens) != 1:
        return None
    return parse_int_literal(value_tokens[0])
