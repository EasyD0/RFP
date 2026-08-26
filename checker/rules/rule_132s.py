"""
checker.rules.rule_132s - 规则 132S: 不能在逻辑表达式中使用赋值.

## 误报模式
逻辑表达式 (if/while/for/do 的条件, 或独立比较表达式) 中出现赋值, 但赋值的
结果不是直接作为布尔值参与逻辑判断, 而是先被某个"取值"运算消费 (比较/算术/
函数实参等), 再得到逻辑值. 这种写法是有意的习惯用法, 判为误报. 典型形式:

    (ret = f(x)) == 0        // 赋值结果被 == 比较
    (ret = f(x)) + 1 > 1     // 赋值结果先做算术, 再被 > 比较

## 判定标准
对逻辑表达式内的每个赋值节点, 沿父链向上 (跳过 PAREN_EXPR / UNEXPOSED_EXPR
包装层), 逐个检查最近的"有意义"消费者节点:

- 比较运算符 (== != < > <= >=)      → "取值"使用, 属于误报模式
- 其它取值运算符 (= + - * / 调用实参 数组下标 ...) → 继续向上, 看结果最终是否流入比较
- 逻辑布尔边界 (&& || ! 三元条件 表达式顶层) → 作为布尔值使用, 不属于误报模式

当且仅当表达式内所有赋值都以"取值"方式被消费, 才判为误报.

### 对 (ret = f(x)) + 1 > 1 的考虑
赋值结果并不直接出现在比较运算符的紧邻操作数里, 而是先经过算术运算 `+` 再参与
比较 `>`. 若只看"赋值是否直接作为比较的操作数", 会漏掉这一类. 因此这里沿父链
向上: 遇到取值运算符 (`+`) 继续上溯, 遇到比较运算符 (`>`) 即判定为"取值使用".
注意若向上先遇到逻辑布尔边界, 则判定为"布尔使用", 不判误报, 例如:

    ((ret = f()) && y) == 1    // 赋值直接是 && 的操作数 → 布尔使用, 不判
    ret = f() == 0             // 顶层赋值, 比较在右侧 → 不判

## 保守性 (无虚警)
- 只分析控制语句的条件部分, 不把同一行循环体/分支里的赋值算进来
- 表达式内所有赋值都必须满足"取值使用"才判; 只要有一个赋值是裸布尔使用
  (如 (ret=f())==0 && (y=g())), 就不判误报
- 解析失败 / 游标为空 → 不判
"""

from clang.cindex import Cursor, CursorKind

from MyPyLib.LogSet import logSetup
from data_structure import Problem

from ..base import Checker, register_checker, tag_padding
from ..context import CodeContext
from clang_tool import get_cursor_in_pos, get_first_ancestor
from clang_tool.ast_utils import _build_parent_map

logger = logSetup(__name__)

# 比较运算符: 赋值结果被比较 → "取值"使用
_COMPARISON_OPS = {"==", "!=", "<", ">", "<=", ">="}
# 逻辑二元运算符: 操作数为布尔 → 赋值被"布尔"使用
_LOGICAL_BIN_OPS = {"&&", "||"}
# 括号 / 未展开包装层: 向上时跳过
_WRAP_KINDS = {CursorKind.PAREN_EXPR, CursorKind.UNEXPOSED_EXPR}
# 控制流语句: 从中提取条件 (逻辑表达式)
_CTRL_KINDS = {
    CursorKind.IF_STMT,
    CursorKind.WHILE_STMT,
    CursorKind.FOR_STMT,
    CursorKind.DO_STMT,
}


def _condition_of(ctrl: Cursor) -> Cursor | None:
    """返回控制语句的条件表达式 (逻辑表达式). libclang 子节点布局:
    if/while: [条件, 体], do: [体, 条件], for: [init, 条件, inc, 体]"""
    kids = list(ctrl.get_children())
    if not kids:
        return None
    if ctrl.kind == CursorKind.DO_STMT:
        return kids[-1]
    if ctrl.kind == CursorKind.FOR_STMT:
        return kids[1] if len(kids) >= 2 else None
    return kids[0]


def _locate_logical_expr(cursor: Cursor, parent_map: dict) -> Cursor | None:
    """
    定位要分析的逻辑表达式:
    - 游标本身是 if/while/for/do → 取条件
    - 游标位于某控制语句的条件内 → 取该条件
    - 游标在循环体/分支里, 或没有控制语句 → 返回 None / 表达式本身
    """
    if cursor.kind in _CTRL_KINDS:
        return _condition_of(cursor)
    ctrl = get_first_ancestor(cursor, kinds=_CTRL_KINDS, parent_map=parent_map)
    if ctrl is not None:
        cond = _condition_of(ctrl)
        if (
            cond is not None
            and get_first_ancestor(
                cursor,
                predicate=lambda c: c.hash == cond.hash,
                include_self=True,
                parent_map=parent_map,
            )
            is not None
        ):
            return cond
        return None
    return cursor


def _collect_assignments(node: Cursor) -> list[Cursor]:
    """收集表达式内所有赋值节点 (简单 `=` 与复合赋值 += 等)"""
    result: list[Cursor] = []

    def visit(cur: Cursor):
        if cur.kind == CursorKind.BINARY_OPERATOR and cur.spelling == "=":
            result.append(cur)
        elif cur.kind == CursorKind.COMPOUND_ASSIGNMENT_OPERATOR:
            result.append(cur)
        for child in cur.get_children():
            visit(child)

    visit(node)
    return result


def _is_logical_not(node: Cursor) -> bool:
    """UNARY_OPERATOR 是否为逻辑非 ! (libclang 中其 spelling 为空, 用 token 判断)"""
    try:
        tokens = [t.spelling for t in node.get_tokens()]
    except Exception:
        tokens = []
    return bool(tokens and tokens[0] == "!")


def _assignment_used_as_value(assign: Cursor, parent_map: dict) -> bool:
    """
    判断赋值结果是否以"取值"方式被消费 (而非直接作为布尔操作数).

    沿父链向上 (跳过括号包装), 逐个遇到:
    - 比较运算符 → 取值使用 (如 (ret=f())+1>1 里的 >)
    - 逻辑布尔边界 (&& || ! 三元条件 表达式顶层) → 布尔使用
    - 其它取值运算符 → 继续向上, 看结果最终是否流入比较
    """
    cur = assign
    while True:
        parent = parent_map.get(cur.hash)
        if parent is None:
            return False
        if parent.kind in _WRAP_KINDS:
            cur = parent
            continue
        if parent.kind == CursorKind.BINARY_OPERATOR:
            op = parent.spelling
            if op in _COMPARISON_OPS:
                return True
            if op in _LOGICAL_BIN_OPS:
                return False
            cur = parent  # 其它二元运算 (= + - * / << >> & | ...) → 取值, 继续向上
            continue
        if parent.kind == CursorKind.UNARY_OPERATOR:
            if _is_logical_not(parent):
                return False
            cur = parent  # 其它一元运算 (- ~ ++ --) → 取值, 继续向上
            continue
        if parent.kind == CursorKind.CONDITIONAL_OPERATOR:
            return False  # ?: 的条件位置 → 布尔使用
        # 其它 (调用实参 / 数组下标 / 语句 / 声明 ...) → 不是布尔边界, 继续向上
        cur = parent
        continue


@register_checker("132S", "不能在逻辑表达式中使用赋值")
class Checker_132S(Checker):
    @tag_padding("<存在有效的逻辑运算>")
    @staticmethod
    def func1(problem: Problem, code_tool: CodeContext) -> Problem:
        """
        逻辑表达式形如 (ret = f(x)) == 0 时, 赋值结果以"取值"方式被比较/算术
        消费, 而非直接作为布尔值, 属于有意的习惯用法, 判为误报.
        """
        code_pos = problem.code_line[0]
        args = code_tool.get_args(problem.file_path1(code_tool.proj_dir))
        cursor = get_cursor_in_pos(code_pos, code_tool.proj_dir, args)
        if cursor is None:
            logger.warning("无法解析游标, 不判误报")
            return problem

        tu = cursor.translation_unit
        if not tu:
            logger.warning("无翻译单元, 不判误报")
            return problem
        parent_map = _build_parent_map(tu.cursor)

        expr = _locate_logical_expr(cursor, parent_map)
        if expr is None:
            return problem

        assignments = _collect_assignments(expr)
        if not assignments:
            return problem

        if all(_assignment_used_as_value(a, parent_map) for a in assignments):
            logger.debug("逻辑表达式内所有赋值均以取值方式使用, 判为误报")
            problem.set_false()
        return problem
