"""
checker.rules.rule_36s - 规则 36S: 函数没有返回语句.
"""

import re
from pathlib import Path

from clang.cindex import Cursor, CursorKind
from MyPyLib.LogSet import logSetup
from data_structure import Problem

from ..base import Checker, register_checker, tag_padding
from ..context import CodeContext
from clang_tool import get_cursor_at_line, get_cursor_in_func, get_first_ancestor, get_parent
from clangd_tool import find_references

logger = logSetup(__name__)


@register_checker("36S", "函数没有返回语句")
class Checker_36S(Checker):
    @tag_padding("<所有引用处都没有使用返回值>")
    @staticmethod
    def func1(problem: Problem, code_tool: CodeContext) -> Problem:
        """
        通过clangd定位所有引用, 然后检查是否所有引用都是直接使用函数
        如果有对函数指针赋值的情况, 则不是误报
        :param problem:
        :param code_tool:
        :return:
        """

        def is_unused_return_call_start(ref_code: str, func_name: str) -> bool:
            """
            判断 ref_code 是否以如下两种形式之一开头 (允许前后及单词之间存在任意空白字符):
                1. (void) func_name(    —— 显式丢弃返回值的函数调用
                2. func_name(           —— 直接的函数调用
            :param ref_code: 引用处的代码片段
            :param func_name: 被调用的函数名
            :return: 命中返回 True, 否则 False
            """
            escaped = re.escape(func_name)  # 避免其中含有正则元字符时误匹配
            pattern = rf"^\s*(?:\(\s*void\s*\)\s*)?{escaped}\s*\("
            return re.match(pattern, ref_code) is not None

        def _is_return_value_used(
            file_path: str, line_0based: int, col_0based: int
        ) -> bool:
            """
            使用 libclang AST 进一步验证返回值是否真的未被使用,
            可正确处理跨行函数调用等复杂情况:
                g(
                 f(x)     // 看似未使用, 实际作为 g 的参数
                )
                g(/* comment */
                 f(x)    // 注释行也不影响 AST 判断
                )

            由于 libclang 对表达式节点不提供 parent 指针,
            复用 clang_tool 的父节点映射表 (get_first_ancestor / get_parent)
            找到引用位置所属的最内层 CallExpr 及其直接父节点, 然后检查:
            - CompoundStmt → 独立语句, 返回值未使用
            - CStyleCastExpr(→void) → 显式丢弃, 返回值未使用
            - 无大括号的控制流语句体 (for/while/do/if/switch/case/default/label)
              如 `for (;;) foo(1);` → 独立语句, 返回值未使用
            - 其他 → 返回值被使用 (作为参数/运算/return等)

            :param file_path: 源文件路径
            :param line_0based: 0-based 行号 (来自 clangd)
            :param col_0based: 0-based 列号 (来自 clangd)
            :return: True 表示返回值被使用, False 表示未使用
            """
            # get_cursor_at_line 使用 1-based 行列
            cursor = get_cursor_at_line(
                Path(file_path), line_0based + 1, col_0based + 1
            )
            if not cursor:
                return False

            # 找到包含引用位置的闭包函数
            in_func = get_cursor_in_func(cursor)
            if not in_func:
                return False

            def _is_unbraced_body_call(parent, call_expr) -> bool:
                """
                判断调用是否位于"无大括号的控制流语句体"位置.
                此时调用作为独立语句执行, 返回值同样被丢弃:
                    for (;;) foo(1);
                    while (c) foo(1);
                    do foo(1); while (c);
                    if (c) foo(1);
                    switch (c) case 1: foo(1);

                libclang 子节点布局 (空槽位会被跳过):
                    if:      [条件, then分支, (else分支)]
                    while:   [条件, 体]          体 = 最后一个
                    do:      [体, 条件]          体 = 第一个
                    for:     [init, cond, inc, 体]  体 = 最后一个
                    switch:  [条件, 体]          体 = 最后一个
                    case/default/label: [表达式?, 体]  体 = 最后一个
                """
                kids = list(parent.get_children())
                if not kids:
                    return False
                start = call_expr.extent.start
                try:
                    idx = next(
                        i
                        for i, k in enumerate(kids)
                        if k.extent
                        and k.extent.start.line == start.line
                        and k.extent.start.column == start.column
                    )
                except StopIteration:
                    return False

                if parent.kind == CursorKind.IF_STMT:
                    # 条件在第一个子节点; then/else 分支中的调用视为未使用
                    return idx != 0
                if parent.kind == CursorKind.DO_STMT:
                    # do-while 的循环体在第一个子节点
                    return idx == 0
                # while/for/switch/case/default/label: 语句体是最后一个子节点
                return idx == len(kids) - 1

            # 通过父节点映射表定位引用位置所在的最内层 CallExpr 及其直接父节点,
            # 替代原先"从函数根向下全树遍历 + 位置比较"的手写实现
            call_expr = get_first_ancestor(
                cursor, kinds={CursorKind.CALL_EXPR}, include_self=True
            )
            parent = get_parent(call_expr) if call_expr else None

            if not call_expr or not parent:
                return False

            # 父节点是 CompoundStmt → 独立语句, 返回值未使用
            if parent.kind == CursorKind.COMPOUND_STMT:
                return False

            # 父节点是 CStyleCastExpr 且转换类型为 void → 显式丢弃
            if parent.kind == CursorKind.CSTYLE_CAST_EXPR:
                if "void" in parent.type.spelling:
                    return False

            # 父节点是控制流语句且调用位于语句体槽位 (无大括号) → 返回值未使用
            if parent.kind in (
                CursorKind.IF_STMT,
                CursorKind.WHILE_STMT,
                CursorKind.DO_STMT,
                CursorKind.FOR_STMT,
                CursorKind.SWITCH_STMT,
                CursorKind.CASE_STMT,
                CursorKind.DEFAULT_STMT,
                CursorKind.LABEL_STMT,
            ) and _is_unbraced_body_call(parent, call_expr):
                return False

            # 其他情况: 返回值被使用了
            return True

        func_name = problem.func_name

        # 定位所有引用该函数的代码
        all_references: list[dict] = find_references(
            code_tool.proj_dir,
            code_tool.compile_dir,
            code_tool.proj_dir / problem.code_line[0].path,
            problem.code_line[0].line - 1,
            problem.code_line[0].token.find(func_name),
        )
        # 检查每一处是否都未使用函数的返回值
        not_use_return_val = 0
        for ref_loc in all_references:
            file_path = ref_loc["uri"]
            start_line = int(ref_loc["start"].get("line", -1))
            if start_line < 0:
                continue

            # 读取该行代码
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                file_lines = f.readlines()
            if start_line >= len(file_lines):
                continue
            ref_code = file_lines[start_line]

            if "=" in ref_code and "==" not in ref_code:
                problem.clear_false()
                problem.pro_des += "存在一处赋值发生, 应该不是误报"
                return problem

            # 检查这行代码是否以单纯函数调用为开头, 并且不是上一行的内部
            if is_unused_return_call_start(ref_code, func_name):
                # 此时说明这个调用处, 返回值可能没有使用, 继续检查
                if _is_return_value_used(
                    file_path, start_line, ref_loc["start"].get("character", 0)
                ):
                    problem.clear_false()
                    logger.debug(f"发现一处使用返回值的引用 {ref_code}")
                    problem.pro_des += f"发现一处使用返回值的引用"
                    return problem
                not_use_return_val += 1
            else:
                problem.clear_false()
                logger.debug(f"发现一处使用返回值的引用 {ref_code}")
                problem.pro_des += f"发现一处使用返回值的引用"
                return problem

        if not_use_return_val == len(all_references):
            problem.set_false()
            return problem

        return problem
