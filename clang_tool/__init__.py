"""
clang_tool - Clang AST 工具包.

对外统一导出公共 API, 保持与原 clang_tool.py 单文件模块兼容,
checker.py 等调用方无需修改 import 语句.

子模块职责:
- config:    libclang 路径 / 编译参数配置 (可用环境变量覆盖)
- parse:     源码解析与游标定位
- ast_utils: AST 结构查询 (父节点 / 祖先 / 块 / 兄弟 / 约束语句)
- value:     整型字面量 / 宏值提取
- demo:      使用示例 (python -m clang_tool.demo)
"""

from .config import CLANG_INC, LIBCLANG_DIR
from .parse import (
    cursor_at,
    find_column,
    get_cursor_at_line,
    get_cursor_in_pos,
    get_cursor_text,
    parse_tu,
)
from .ast_utils import (
    get_ancestors,
    get_constraint,
    get_cursor_in_func,
    get_first_ancestor,
    get_innermost_block,
    get_parent,
    get_parent_node,
    get_same_level_nodes,
)
from .value import (
    get_macro_int_value,
    literal_value_from_cursor,
    parse_int_literal,
)

__all__ = [
    # config
    "CLANG_INC",
    "LIBCLANG_DIR",
    # parse
    "parse_tu",
    "cursor_at",
    "get_cursor_at_line",
    "get_cursor_in_pos",
    "find_column",
    "get_cursor_text",
    # ast_utils
    "get_ancestors",
    "get_parent",
    "get_first_ancestor",
    "get_parent_node",
    "get_constraint",
    "get_cursor_in_func",
    "get_innermost_block",
    "get_same_level_nodes",
    # value
    "parse_int_literal",
    "literal_value_from_cursor",
    "get_macro_int_value",
]
