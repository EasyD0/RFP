"""
checker.rules.rule_47s - 规则 47S: 数组越界.
"""

import re

from clang.cindex import Cursor, CursorKind
from MyPyLib.LogSet import logSetup
from data_structure import Problem

from ..base import Checker, register_checker, tag_padding
from ..context import CodeContext
from clang_tool import (
    cursor_at,
    get_first_ancestor,
    get_macro_int_value,
    get_parent_node,
    literal_value_from_cursor,
    parse_tu,
)

logger = logSetup(__name__)


@register_checker("47S", "数组越界")
class Checker_47S(Checker):
    """
    47S规则的报告形如:
    代码行:
    CES/CESFilterCount/src/Filter_CounterCfg.c:233
    gstSlow_Start_Stop_Flag[ucChannel] = 3;

    规则名称:
    数组下标越界 : gstSlow_Start_Stop_Flag[*]; accessed=4, range=0-3
    """

    @tag_padding("<发现充分的下标约束>")
    @staticmethod
    def func1(problem: Problem, code_tool: CodeContext) -> Problem:
        """
        只做简单的单层检查, 不考虑复杂的数据流逻辑
        获取数组节点, 确定数组长度
        获取发生越界的语句节点A,
        然后在函数里找到语句A的父节点, 检查它的父节点里是否有可以防止越界的约束, 如果有则视为误报
        比如说:
            if(i<2&&i>=0){
                arr[i];  //它的父节点里的约束是 i<2&&i>=0
            }

        :param problem:
        :param code_tool:
        :return:
        """

        if "[][*]" in problem.rule_name:
            logger.debug("多层数组不检查")
            return problem
        if not "[" in problem.rule_name:
            logger.debug("不是普通数组, 不检查")
            return problem

        # 提取数组名称
        arr_name = problem.rule_name.token.split("[*]")[0]
        if not re.search(r"\[|\.|->", arr_name):
            # 数组不是 数组元素 结构体成员 或者 指针指向的对象
            arr_name = arr_name.strip()
        elif not "[" in arr_name:
            arr_name = re.split(r"->|\.", arr_name)[-1]
        else:
            arr_name = None
        if not arr_name:
            logger.warning("无法识别数组名称")
            return problem

        logger.debug(f"数组名称为 {arr_name}")

        source_file_abs = problem.file_path1(code_tool.proj_dir)
        with open(source_file_abs, "r", errors="replace") as f:
            raw_code_line_text = f.readlines()[problem.code_line[0].line - 1]

        src_path = code_tool.proj_dir / problem.code_line[0].path
        line_num = problem.code_line[0].line
        clangd_args = code_tool.get_args(problem.code_line[0].path)

        # 只解析一次翻译单元, 同一行的数组/下标游标都从它获取
        tu = parse_tu(src_path, clangd_args)
        if tu is None:
            logger.warning("解析失败")
            return problem

        def _is_ident_char(ch) -> bool:
            return ch is not None and (ch.isalnum() or ch == "_")

        def _unwrap_expr(cur: Cursor) -> Cursor:
            # 去掉 libclang 的 UNEXPOSED_EXPR 包装层, 拿到实际表达式节点
            while cur is not None and cur.kind == CursorKind.UNEXPOSED_EXPR:
                kids = list(cur.get_children())
                if len(kids) != 1:
                    break
                cur = kids[0]
            return cur

        def _find_array_accesses() -> list[tuple[Cursor, Cursor]]:
            """
            通过 AST 找出该行中所有形如 arr_name[...] 的访问。
            返回 [(数组游标, 下标表达式游标)], 下标已去掉 UNEXPOSED_EXPR 包装。
            """
            accesses = []
            search_from = 0
            while True:
                pos = raw_code_line_text.find(arr_name, search_from)
                if pos < 0:
                    break
                search_from = pos + 1
                # 边界检查: 不能是更长标识符的一部分 (如 barr)
                if _is_ident_char(raw_code_line_text[pos - 1] if pos > 0 else None):
                    continue
                after = pos + len(arr_name)
                if _is_ident_char(
                    raw_code_line_text[after]
                    if after < len(raw_code_line_text)
                    else None
                ):
                    continue

                arr_cursor = cursor_at(tu, src_path, line_num, pos + 1)
                if arr_cursor is None:
                    continue
                subscript = get_first_ancestor(
                    arr_cursor, kinds={CursorKind.ARRAY_SUBSCRIPT_EXPR}
                )
                if subscript is None:
                    continue
                kids = list(subscript.get_children())
                if not kids:
                    continue
                # ARRAY_SUBSCRIPT_EXPR 子节点布局: [数组基址, 下标表达式]
                accesses.append((arr_cursor, _unwrap_expr(kids[-1])))
            return accesses

        accesses = _find_array_accesses()
        if not accesses:
            logger.warning("无法在代码行中定位数组访问")
            return problem

        # 下标必须是简单表达式 (单个变量/字面量/宏), 复杂表达式 (如 i+1) 保守退出
        _SIMPLE_INDEX_KINDS = {
            CursorKind.INTEGER_LITERAL,
            CursorKind.DECL_REF_EXPR,
            CursorKind.MACRO_INSTANTIATION,
        }
        if any(idx.kind not in _SIMPLE_INDEX_KINDS for _, idx in accesses):
            logger.warning("下标情况复杂, 不再检查直接退出")
            return problem

        # 同一行出现多种不同下标 (如 arr[i] + arr[j]) 时保守退出
        index_texts: set[str | None] = set()
        for _, idx in accesses:
            s, e = idx.extent.start, idx.extent.end
            if s.line != line_num or e.line != line_num:
                index_texts.add(None)  # 跨行下标按复杂处理
            else:
                index_texts.add(
                    raw_code_line_text[s.column - 1 : e.column - 1].strip()
                )
        if len(index_texts) != 1 or None in index_texts:
            logger.warning("下标情况复杂, 不再检查直接退出")
            return problem

        arr_cursor, idx_cursor = accesses[0]

        if arr_cursor.kind not in {
            CursorKind.DECL_REF_EXPR,
            CursorKind.VAR_DECL,
            CursorKind.PARM_DECL,
        }:
            logger.warning("数组节点查找错误")
            return problem

        # 求解数组长度
        arr_size: int = arr_cursor.type.get_canonical().get_array_size()
        if not isinstance(arr_size, int) or arr_size < 1:
            logger.error("数组长度获取失败")
            return problem

        # 部分 libclang 版本里 INTEGER_LITERAL.spelling 为空,
        # 改用 token 文本 / 源码 extent 取值
        if "literal" in str(idx_cursor.kind).lower():
            idx_value = literal_value_from_cursor(idx_cursor)
            if idx_value is None:
                # 宏展开出的字面量 (如 #define N 3 后写 arr[N]) 的 extent 落在宏名上,
                # 源码文本不是数字, 退回宏定义解析
                idx_value = get_macro_int_value(
                    src_path, line_num, idx_cursor.extent.start.column, clangd_args
                )
            if idx_value is None:
                logger.warning("无法处理数字下标的字面量值")
                return problem
            # 访问确实在界内 (0 <= idx < arr_size) 才算误报
            if 0 <= idx_value < arr_size:
                problem.set_false()
                return problem
        elif idx_cursor.kind == CursorKind.MACRO_INSTANTIATION:
            # 宏展开下标 (如 arr[N]): 尝试从宏定义解析字面值
            idx_value = get_macro_int_value(
                src_path, line_num, idx_cursor.extent.start.column, clangd_args
            )
            if idx_value is not None and 0 <= idx_value < arr_size:
                problem.set_false()
                return problem
            # 宏值未知/替换列表复杂, 或访问越界 → 保守不判误报
            return problem
        elif (
            idx_cursor.get_definition()
            and idx_cursor.get_definition().kind == CursorKind.ENUM_CONSTANT_DECL
        ):
            # 是枚举引用
            idx_value = idx_cursor.get_definition().enum_value
            if 0 <= idx_value < arr_size:
                problem.set_false()
                return problem
        elif idx_cursor.kind == CursorKind.DECL_REF_EXPR:
            # 下标是变量的情况, 需要检查上层中的约束, 比如
            # for (i = 0; i < 10; ++i) {f(x); arr[i];}
            # 先定位到父节点 for (i = 0; i < 10; ++i), 再检查约束是否充分
            parent_node = get_parent_node(idx_cursor)
            if not parent_node:
                # 找不到约束节点, 无法证明访问在界内, 保守不判误报
                logger.debug("未找到下标变量的约束语句, 保守退出")
                return problem
            # TODO: 后续用 get_constraint(parent_node, idx_cursor) 分析约束是否充分
            return problem

        return problem
