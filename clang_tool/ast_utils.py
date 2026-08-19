"""
clang_tool.ast_utils - AST 结构查询 (父节点 / 祖先 / 块作用域 / 兄弟语句).

libclang 对表达式节点不提供 parent 指针, 因此这里通过遍历子节点建立
"父节点映射表", 在此基础上提供统一的祖先 / 父节点查询原语:

- get_ancestors / get_parent / get_first_ancestor: 通用查询
- get_parent_node: 最近的约束语句 (for / while / if / switch 等)
- get_cursor_in_func / get_innermost_block / get_same_level_nodes: 便捷封装
- get_constraint: 约束提取 (待实现)
"""

from typing import Callable

from clang.cindex import Cursor, CursorKind


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


def get_constraint(expr_node: Cursor, idx_cur: Cursor) -> set[str] | None:
    """
    获取一个表达式中的约束, 比如 for(i = 1; i < 10; ++i) 的 i<10。
    TODO: 待实现, 可基于 get_parent_node 定位约束语句后提取其条件/边界。

    :param expr_node: 约束语句节点 (如 for)
    :param idx_cur: 目标表达式游标 (如下标变量)
    :return: 约束集合或 None
    """
    pass
