"""
clang_tool.ast_utils - AST 结构查询 (父节点 / 祖先 / 块作用域 / 兄弟语句 / 约束提取).

libclang 对表达式节点不提供 parent 指针, 因此这里通过遍历子节点建立
"父节点映射表", 在此基础上提供统一的祖先 / 父节点查询原语:

- get_ancestors / get_parent / get_first_ancestor: 通用查询
- get_parent_node: 最近的约束语句 (for / while / if / switch 等)
- get_cursor_in_func / get_innermost_block / get_same_level_nodes: 便捷封装
- get_constraint: 约束提取 (for / while / if 等条件中的比较, 以及 for 初始化边界)
"""

import re
from pathlib import Path
from typing import Callable

from clang.cindex import Cursor, CursorKind, TypeKind

from .value import literal_value_from_cursor, parse_int_literal


# 约束提取中可用的比较运算符 (与数组界内判断相关)
_BOUND_OPS = {"<", "<=", ">", ">=", "=="}

# 无符号整型种类 (无符号类型天然满足 i >= 0)
_UNSIGNED_INT_KINDS = frozenset(
    getattr(TypeKind, n)
    for n in ("UCHAR", "USHORT", "UINT", "ULONG", "ULONGLONG", "UINT128")
    if hasattr(TypeKind, n)
)

# 有符号整型种类
_SIGNED_INT_KINDS = frozenset(
    getattr(TypeKind, n)
    for n in ("CHAR_S", "SChar", "SHORT", "INT", "LONG", "LONGLONG", "INT128")
    if hasattr(TypeKind, n)
)


def _loc_key(loc) -> tuple[int, int]:
    # 用于比较源码位置
    return (loc.line, loc.column)


# 控制流语句类型 - 这些是"完整语句", 也是约束分析的定位目标
_CONSTRAINT_STATEMENT_KINDS = {
    CursorKind.FOR_STMT,
    CursorKind.WHILE_STMT,
    CursorKind.DO_STMT,
    CursorKind.IF_STMT,
    CursorKind.SWITCH_STMT,
    CursorKind.CASE_STMT,
    CursorKind.DEFAULT_STMT,
    CursorKind.LABEL_STMT,
}


def _build_parent_map(root_cursor: Cursor) -> dict:
    """
    从根节点开始遍历, 记录每个子节点的父节点。用 cursor.hash 做 key。

    :param root_cursor: AST 根节点
    :return: dict, key 为 cursor.hash, value 为父节点
    """
    parent_map = {}

    def visit(cur: Cursor, parent: Cursor | None):
        if parent is not None:
            parent_map[cur.hash] = parent
        for child in cur.get_children():
            visit(child, cur)

    visit(root_cursor, None)
    return parent_map


def _get_ancestors(cursor: Cursor, parent_map: dict) -> list[Cursor]:
    """从近到远返回 cursor 的所有祖先节点 (不含自身)."""
    ancestors = []
    cur = cursor
    while True:
        p = parent_map.get(cur.hash)
        if p is None:
            break
        ancestors.append(p)
        cur = p
    return ancestors


def _parent_map_for(node: Cursor) -> dict:
    """返回 node 所在翻译单元的父节点映射表; 无法获取时返回空表"""
    tu = node.translation_unit
    if not tu:
        return {}
    return _build_parent_map(tu.cursor)


def get_ancestors(node: Cursor, parent_map: dict | None = None) -> list[Cursor]:
    """
    返回 node 的所有祖先节点, 从近到远 (不含 node 自身)。

    :param node: 目标节点的游标
    :param parent_map: 可复用的父节点映射表 (由 _build_parent_map 构建),
                      不传则基于 node 所在翻译单元内部构建
    :return: 祖先节点列表 (从近到远)
    """
    if parent_map is None:
        parent_map = _parent_map_for(node)
    return _get_ancestors(node, parent_map)


def get_parent(node: Cursor, parent_map: dict | None = None) -> Cursor | None:
    """
    返回 node 的直接(词法)父节点。

    libclang 对表达式节点不提供 parent 指针, 这里通过父节点映射表查询。
    :param node: 目标节点的游标
    :param parent_map: 可复用的父节点映射表, 不传则内部构建
    :return: 直接父节点; 无法确定时返回 None
    """
    if parent_map is None:
        parent_map = _parent_map_for(node)
    return parent_map.get(node.hash)


def get_first_ancestor(
    node: Cursor,
    kinds: set[CursorKind] | None = None,
    predicate: Callable[[Cursor], bool] | None = None,
    include_self: bool = False,
    parent_map: dict | None = None,
) -> Cursor | None:
    """
    从 node 自身 (include_self=True 时) 或最近祖先开始, 向上找到第一个满足条件的节点。

    :param node: 起始游标
    :param kinds: 要匹配的 CursorKind 集合; 与 predicate 同时提供时, 任一命中即可
    :param predicate: 自定义匹配函数, 接收 Cursor 返回 bool
    :param include_self: 是否把 node 自身也纳入匹配 (默认只查祖先)
    :param parent_map: 可复用的父节点映射表, 不传则内部构建
    :return: 第一个命中的节点; 找不到返回 None
    """
    if kinds is None and predicate is None:
        raise ValueError("kinds 和 predicate 至少需要提供一个")
    if parent_map is None:
        parent_map = _parent_map_for(node)

    def _match(cur: Cursor) -> bool:
        if kinds is not None and cur.kind in kinds:
            return True
        if predicate is not None and predicate(cur):
            return True
        return False

    if include_self and _match(node):
        return node
    for ancestor in _get_ancestors(node, parent_map):
        if _match(ancestor):
            return ancestor
    return None


def get_parent_node(node: Cursor) -> Cursor | None:
    """
    返回包含 node 所在语句的、最近的具有约束性质的语句节点
    (for / while / do / if / switch 等), 供后续约束分析使用
    (例如提取 for 的循环边界 i < 10)。

    比如
        for(i = 1; i < 10; ++i) {arr[i];}

    传入 arr 的游标 (arr 位于语句 arr[i]; 中), 返回 for 的游标。

    特殊情形:
    - 若 node 本身就是约束语句 (例如 Cursor.from_location 落在关键字/空白处时
      返回的是整个语句), 则直接返回 node 自身;
    - 若 node 位于 for / if 等语句头部的括号内 (如条件中的 i), 该 for / if
      本身就是包含它的完整语句, 同样返回该 for / if。

    :param node: 目标节点的游标
    :return: 最近的约束语句节点, 如果没有则返回 None
    """
    return get_first_ancestor(
        node,
        kinds=_CONSTRAINT_STATEMENT_KINDS,
        include_self=True,
    )


def get_cursor_in_func(node: Cursor) -> Cursor | None:
    """找到包含 node 的函数定义节点 (最近的 FUNCTION_DECL 祖先)"""
    return get_first_ancestor(node, kinds={CursorKind.FUNCTION_DECL})


def get_innermost_block(node: Cursor) -> Cursor | None:
    """
    找到包含 node 的最内层复合语句块, 即 node 所在块作用域。
    基于父节点映射表向上查找, 等价于旧版"全树遍历 + 源码范围比较"。
    """
    return get_first_ancestor(node, kinds={CursorKind.COMPOUND_STMT})


def get_same_level_nodes(node: Cursor) -> list[Cursor]:
    """
    收集与 node 同一层级、且起始位置不晚于 node 位置的兄弟语句。
    TODO 这里可能有问题, 如果 if for 等语句后面没有 {
    """
    block = get_innermost_block(node)
    if block is None:
        return []
    pos = _loc_key(node.extent.start)
    result = []
    for child in block.get_children():
        if str(child.extent.start.file) != str(node.extent.start.file):
            continue
        if _loc_key(child.extent.start) <= pos:
            result.append(child)
    return result


def _unwrap_expr(cur: Cursor) -> Cursor:
    """去掉 UNEXPOSED_EXPR / PAREN_EXPR 包装层, 拿到实际表达式节点"""
    while cur is not None and cur.kind in (
        CursorKind.UNEXPOSED_EXPR,
        CursorKind.PAREN_EXPR,
    ):
        kids = list(cur.get_children())
        if len(kids) != 1:
            break
        cur = kids[0]
    return cur


def _is_idx_ref(cur: Cursor, idx_name: str) -> bool:
    """判断表达式是否为对 idx_name 变量的引用"""
    cur = _unwrap_expr(cur)
    return cur.kind == CursorKind.DECL_REF_EXPR and cur.spelling == idx_name


def _macro_int_value(cur: Cursor) -> int | None:
    """解析简单对象宏 (如 #define N (10)) 的整型字面量值, 失败返回 None"""
    definition = cur.get_definition()
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
    while (
        len(value_tokens) >= 2
        and value_tokens[0] == "("
        and value_tokens[-1] == ")"
    ):
        value_tokens = value_tokens[1:-1]
    if len(value_tokens) != 1:
        return None
    return parse_int_literal(value_tokens[0])


def _literal_macro_fallback(cur: Cursor) -> int | None:
    """
    部分 libclang 版本会把对象宏 (如 #define N 5) 展开成 INTEGER_LITERAL,
    且其 extent 落在宏名上, 源码文本不是数字. 此时在宏名所在文件内
    从后往前查找最后一个 #define <宏名> <整型字面量> 作为兜底解析.
    命令行宏 (-DN=5) 或宏定义在其他文件时无法解析, 返回 None.
    """
    s, e = cur.extent.start, cur.extent.end
    if s.line != e.line or e.column <= s.column:
        return None
    file_path = Path(str(s.file))
    try:
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return None
    if not (1 <= s.line <= len(lines)):
        return None
    line_text = lines[s.line - 1]
    line_bytes = line_text.encode("utf-8", errors="ignore")
    macro_name = line_bytes[s.column - 1 : e.column - 1].decode(
        "utf-8", errors="ignore"
    )
    if not re.fullmatch(r"[A-Za-z_]\w*", macro_name):
        return None
    pattern = re.compile(
        rf"^\s*#define\s+{re.escape(macro_name)}\b(.*?)(?://.*|/\*.*)?$"
    )
    undef_pattern = re.compile(rf"^\s*#\s*undef\s+{re.escape(macro_name)}\b")
    for line in reversed(lines):
        if undef_pattern.match(line):
            return None
        m = pattern.match(line)
        if not m:
            continue
        value_tokens = m.group(1).split()
        while (
            len(value_tokens) >= 2
            and value_tokens[0] == "("
            and value_tokens[-1] == ")"
        ):
            value_tokens = value_tokens[1:-1]
        if len(value_tokens) != 1:
            return None
        return parse_int_literal(value_tokens[0])
    return None


def _constant_int(cur: Cursor) -> int | None:
    """
    求表达式的整型常量值: 支持整型字面量、简单对象宏、枚举常量、
    以及 const 修饰的整型变量; 无法确定时返回 None。
    """
    cur = _unwrap_expr(cur)
    kind = cur.kind
    if kind == CursorKind.INTEGER_LITERAL:
        value = literal_value_from_cursor(cur)
        if value is not None:
            return value
        # 对象宏展开出的字面量, extent 落在宏名上, 退回宏定义文本解析
        return _literal_macro_fallback(cur)
    if kind == CursorKind.MACRO_INSTANTIATION:
        return _macro_int_value(cur)
    if kind == CursorKind.DECL_REF_EXPR:
        definition = cur.get_definition()
        if not definition:
            return None
        if definition.kind == CursorKind.ENUM_CONSTANT_DECL:
            return definition.enum_value
        if definition.kind == CursorKind.VAR_DECL:
            # const int N = 10; 这类编译期常量
            is_const = getattr(definition.type, "is_const_qualified", None)
            if not is_const or not is_const():
                return None
            init_kids = list(definition.get_children())
            if len(init_kids) == 1:
                return _constant_int(init_kids[0])
    if kind == CursorKind.UNARY_OPERATOR:
        # -1 / +1: 负号在 AST 中是 UNARY_OPERATOR 包着整型字面量
        kids = list(cur.get_children())
        if len(kids) == 1:
            try:
                tokens = [t.spelling for t in cur.get_tokens()]
            except Exception:
                tokens = []
            if "-" in tokens:
                value = _constant_int(kids[0])
                return -value if value is not None else None
            if "+" in tokens:
                return _constant_int(kids[0])
    return None


def _collect_comparisons(expr: Cursor, idx_name: str) -> set[str] | None:
    """
    递归收集条件表达式 expr 中所有与 idx_name 相关的比较约束。

    只支持:
    - && 连接: 各子表达式都是约束 (i >= 0 && i < 10)
    - 单边为 idx_name、另一边为整型常量的比较 (i < 10 / 0 <= i / i == 3)
    含 || / 取反 / 函数调用等无法保守拆分的逻辑时, 对应子树返回 None 并跳过。
    """
    expr = _unwrap_expr(expr)
    if expr.kind != CursorKind.BINARY_OPERATOR:
        return None
    op = expr.spelling
    kids = list(expr.get_children())
    if len(kids) != 2:
        return None
    if op == "&&":
        result: set[str] = set()
        for kid in kids:
            sub = _collect_comparisons(kid, idx_name)
            if sub:
                result |= sub
        return result
    if op == "||":
        return None
    if op not in _BOUND_OPS:
        return None
    lhs, rhs = _unwrap_expr(kids[0]), _unwrap_expr(kids[1])
    if _is_idx_ref(lhs, idx_name):
        value = _constant_int(rhs)
        if value is None:
            return None
        return {f"{idx_name} {op} {value}"}
    if _is_idx_ref(rhs, idx_name):
        value = _constant_int(lhs)
        if value is None:
            return None
        # 变量恒归一化到左侧: 0 <= i  →  i >= 0,  5 > i  →  i < 5
        flipped = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "==": "=="}[op]
        return {f"{idx_name} {flipped} {value}"}
    return None


def _is_signed_integer_type(type) -> bool:
    """判断类型是否为有符号整型 (用于判断循环边界推导是否安全)"""
    canonical = type.get_canonical()
    if canonical.kind in _UNSIGNED_INT_KINDS:
        return False
    return canonical.kind in _SIGNED_INT_KINDS


def _increment_direction(inc: Cursor | None, idx_name: str) -> int:
    """
    判断循环递进语句对 idx_name 的影响方向:
    1 = 非递减 (++i / i++ / i += k>0 / i = i + k), -1 = 非递增, 0 = 未知.
    """
    if inc is None:
        return 0
    kind = inc.kind
    if kind == CursorKind.UNARY_OPERATOR:
        try:
            tokens = [t.spelling for t in inc.get_tokens()]
        except Exception:
            return 0
        op = next((t for t in tokens if t in ("++", "--")), None)
        kids = list(inc.get_children())
        if len(kids) != 1 or not _is_idx_ref(kids[0], idx_name):
            return 0
        if op == "++":
            return 1
        if op == "--":
            return -1
        return 0
    if kind == CursorKind.COMPOUND_ASSIGNMENT_OPERATOR:
        op = inc.spelling
        if op not in ("+=", "-="):
            return 0
        kids = list(inc.get_children())
        if len(kids) != 2 or not _is_idx_ref(kids[0], idx_name):
            return 0
        delta = _constant_int(kids[1])
        if delta is None or delta == 0:
            return 0
        if op == "+=":
            return 1 if delta > 0 else -1
        return -1 if delta > 0 else 1
    if kind == CursorKind.BINARY_OPERATOR and inc.spelling == "=":
        # i = i + k 或 i = k + i
        kids = list(inc.get_children())
        if len(kids) != 2 or not _is_idx_ref(kids[0], idx_name):
            return 0
        rhs = _unwrap_expr(kids[1])
        if rhs.kind != CursorKind.BINARY_OPERATOR or rhs.spelling != "+":
            return 0
        rkids = list(rhs.get_children())
        if len(rkids) != 2:
            return 0
        left, right = _unwrap_expr(rkids[0]), _unwrap_expr(rkids[1])
        if _is_idx_ref(left, idx_name):
            delta = _constant_int(right)
        elif _is_idx_ref(right, idx_name):
            delta = _constant_int(left)
        else:
            return 0
        if delta is None or delta == 0:
            return 0
        return 1 if delta > 0 else -1
    return 0


def _for_condition(kids: list[Cursor]) -> Cursor | None:
    """
    从 for 的子节点中识别条件表达式. for 的 init/inc/cond 在缺少时会缺位,
    因此按角色识别: 末尾是循环体, 开头是 init, 末尾前一个是 inc, 剩下的是条件.
    """
    if not kids:
        return None
    body = kids[-1]
    init = None
    if kids[0].kind == CursorKind.DECL_STMT or (
        kids[0].kind == CursorKind.BINARY_OPERATOR and kids[0].spelling == "="
    ):
        init = kids[0]
    inc = None
    if len(kids) >= 3:
        maybe_inc = kids[-2]
        if maybe_inc.kind in (
            CursorKind.UNARY_OPERATOR,
            CursorKind.COMPOUND_ASSIGNMENT_OPERATOR,
        ) or (
            maybe_inc.kind == CursorKind.BINARY_OPERATOR
            and maybe_inc.spelling in {"=", "+=", "-="}
        ):
            inc = maybe_inc
    candidates = [k for k in kids if k is not body and k is not init and k is not inc]
    if len(candidates) != 1:
        return None
    return candidates[0]


def _for_init_bound(for_stmt: Cursor, idx_cur: Cursor) -> set[str]:
    """
    从 for 初始化 i = C 推导循环体入口处的边界约束:
    - 非递减递进 (++i 等) → i >= C (提供下界)
    - 非递增递进 (--i 等) → i <= C (提供上界)

    仅对"有符号整型"推导: 无符号整型可能从 0 回绕到最大值, 边界不成立.
    循环体内对 i 的修改由调用方另行保守检查, 不在此处理.
    """
    idx_name = idx_cur.spelling
    kids = list(for_stmt.get_children())
    if not kids or len(kids) < 2:
        return set()
    init = kids[0]
    value = None
    if init.kind == CursorKind.DECL_STMT:
        # for (int i = C; ...)
        for child in init.get_children():
            if child.kind == CursorKind.VAR_DECL and child.spelling == idx_name:
                init_kids = list(child.get_children())
                if len(init_kids) == 1:
                    value = _constant_int(init_kids[0])
                break
    elif init.kind == CursorKind.BINARY_OPERATOR and init.spelling == "=":
        # for (i = C; ...)
        assign_kids = list(init.get_children())
        if len(assign_kids) == 2 and _is_idx_ref(assign_kids[0], idx_name):
            value = _constant_int(assign_kids[1])
    if value is None or not _is_signed_integer_type(idx_cur.type):
        return set()
    inc = kids[-2] if len(kids) >= 2 else None
    direction = _increment_direction(inc, idx_name)
    if direction > 0:
        return {f"{idx_name} >= {value}"}
    if direction < 0:
        return {f"{idx_name} <= {value}"}
    return set()


def _containing_child_index(
    parent: Cursor, node: Cursor, parent_map: dict
) -> int | None:
    """
    返回 parent 的直接子节点中、包含 node 的那个子节点的下标.
    用于判断访问位于约束语句的哪个部分 (循环体 / if 的 then / else 等).
    """
    for anc in _get_ancestors(node, parent_map):
        p = parent_map.get(anc.hash)
        if p is not None and p.hash == parent.hash:
            for i, ch in enumerate(parent.get_children()):
                if ch.hash == anc.hash:
                    return i
            return None
    return None


def get_constraint(expr_node: Cursor, idx_cur: Cursor) -> set[str] | None:
    """
    获取约束语句 expr_node 中对下标变量 idx_cur 的有效约束集合.

    支持:
    - for: 循环条件中的比较 (i < 10), 以及初始化 i = C 在递增/递减循环下
      推导出的边界 (i >= C / i <= C); 仅当下标访问位于循环体内时有效
    - while: 循环条件中的比较; 仅当下标访问位于循环体内时有效
    - if: 条件中的比较; 仅当下标访问位于 then 分支时有效 (else 分支条件不成立)

    约束统一归一化为 "变量 运算符 整数值" (变量恒在左侧), 例如
    {"i < 10", "i >= 0"}. 语句类型/访问位置不受支持时返回 None;
    条件存在但没有可用约束时返回空集合.

    注意: 只做单层检查, 不追踪循环体内对下标变量的修改 (由调用方保守处理),
    也不组合外层约束语句的约束.

    :param expr_node: 约束语句节点 (由 get_parent_node 定位)
    :param idx_cur: 目标表达式游标 (如下标变量)
    :return: 约束集合或 None
    """
    if expr_node is None or idx_cur is None:
        return None
    idx_name = idx_cur.spelling
    if not idx_name:
        return None
    parent_map = _parent_map_for(idx_cur)
    kind = expr_node.kind

    if kind == CursorKind.FOR_STMT:
        kids = list(expr_node.get_children())
        if not kids:
            return None
        pos = _containing_child_index(expr_node, idx_cur, parent_map)
        if pos != len(kids) - 1:
            # 访问不在循环体内 (在条件/递进中), 约束不成立
            return None
        result: set[str] = set()
        cond = _for_condition(kids)
        if cond is not None:
            sub = _collect_comparisons(cond, idx_name)
            if sub:
                result |= sub
        result |= _for_init_bound(expr_node, idx_cur)
        return result

    if kind == CursorKind.WHILE_STMT:
        kids = list(expr_node.get_children())
        if len(kids) < 2:
            return None
        pos = _containing_child_index(expr_node, idx_cur, parent_map)
        if pos != len(kids) - 1:
            return None
        return _collect_comparisons(kids[0], idx_name) or set()

    if kind == CursorKind.IF_STMT:
        kids = list(expr_node.get_children())
        if len(kids) < 2:
            return None
        pos = _containing_child_index(expr_node, idx_cur, parent_map)
        if pos != 1:
            # 访问在条件或 else 分支中, 条件不成立
            return None
        return _collect_comparisons(kids[0], idx_name) or set()

    return None
