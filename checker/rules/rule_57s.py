"""
checker.rules.rule_57s - 规则 57S: 无作用的语句.
"""

import re
from pathlib import Path

from clang.cindex import Cursor, CursorKind, Type
from MyPyLib.LogSet import logSetup
from data_structure import Problem

from ..base import Checker, common_method, register_checker, tag_padding
from ..context import CodeContext
from clang_tool import get_cursor_in_pos

logger = logSetup(__name__)


@register_checker("57S", "无作用的语句")
class Checker_57S(Checker):

    @common_method
    @tag_padding("<未用的参数>")
    @staticmethod
    def func0(problem: Problem, code_tool: CodeContext) -> Problem:
        first_line = problem.code_line[0].token.upper()
        if re.search(r"UNUSED_PARAMETER *\(", first_line):
            problem.set_false()
        return problem

    @common_method
    @tag_padding("<日志打印>")
    @staticmethod
    def func1(problem: Problem, code_tool: CodeContext) -> Problem:
        first_line = problem.code_line[0].token.lower()
        first_line = first_line.split(r"(")[0]
        if "log" in first_line or "print" in first_line:
            problem.set_false()
        return problem

    @common_method
    @tag_padding("<置为void>")
    @staticmethod
    def func2(problem: Problem, code_tool: CodeContext) -> Problem:
        first_line = problem.code_line[0]
        if re.search(r"\( *void *\)", first_line.token):
            problem.set_false()
        return problem

    @common_method
    @tag_padding("<识别为汇编语言>")
    @staticmethod
    def func3(problem: Problem, code_tool: CodeContext) -> Problem:
        """
        这种情况违规的代码行形如
            DRV/MCU_Source/McuDrvCfg/EcuMCfg.c:679
            st.w r7, 0 [r6]

            McuDrv/Abeos/platform.c:102
            andi 0x0020, r10, r10


        汇编代码形如
        #pragma inline_asm  switch_trap_0
        static void switch_trap_0(void){
            trap 0
        }

        但是也有可能形如
        __asm{
            /*汇编代码*/
        }

        实现方法1: 在那个代码行向上定位到一行中带有"{" 然后查找上面是否有
        #pragma inline_asm 或者 __asm

        实现方法2: 检查 token 中的代码文本是否为汇编语言
        """
        line_number = problem.code_line[0].line
        source_file: Path = problem.file_path1(code_tool.proj_dir)
        func_name = problem.func_name
        with open(source_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        file_text = "".join(all_lines)
        if func_name and re.search(
            rf"(?:^|\n)#pragma\s+inline_asm\s+{func_name}", file_text
        ):
            problem.set_false()
            return problem

        i = line_number - 1
        while 0 <= i < len(all_lines):
            cur_code = all_lines[i]
            if "{" in cur_code:
                break
            else:
                i -= 1

        if (i - 1 >= 0) and ("__asm" in all_lines[i - 1]):
            problem.set_false()

        return problem

    @tag_padding("<声明语句>")
    @staticmethod
    def func4(problem: Problem, code_tool: CodeContext) -> Problem:
        args = code_tool.get_args(problem.file_path1(code_tool.proj_dir))
        cursor: Cursor | None = get_cursor_in_pos(
            problem.code_line[0], code_tool.proj_dir, args
        )
        if not cursor:
            logger.warning("无法解析")
        else:
            problem.is_false_alarm = cursor.kind == CursorKind.VAR_DECL
        return problem

    @tag_padding("<调用函数>")
    @staticmethod
    def func5(problem: Problem, code_tool: CodeContext) -> Problem:
        """
        若检测到当且cursor所在的语句是一个函数调用语句, 则判断为误报
        """

        def _is_function_pointer_type(ty: Type) -> bool:
            """判断类型是否为函数指针 (如 int (*)(int)), 而非数组指针等"""
            from clang.cindex import TypeKind

            if ty.kind == TypeKind.POINTER:
                pointee = ty.get_pointee()
                return pointee.kind in (
                    TypeKind.FUNCTIONPROTO,
                    TypeKind.FUNCTIONNOPROTO,
                )
            elif (
                ty.get_canonical()
                and ty.get_canonical().spelling
                and "(*)" in ty.get_canonical().spelling
            ):
                return True
            return False

        args = code_tool.get_args(problem.file_path1(code_tool.proj_dir))
        cursor: Cursor | None = get_cursor_in_pos(
            problem.code_line[0], code_tool.proj_dir, args
        )
        if not cursor:
            logger.warning("无法解析")
            return problem

        flag = False
        if cursor.kind == CursorKind.DECL_REF_EXPR:
            # DECL_REF_EXPR: 直接引用函数名或函数指针变量, 如 foo 或 fp
            definition = cursor.get_definition()
            if definition is not None and definition.kind == CursorKind.FUNCTION_DECL:
                flag = True
            elif _is_function_pointer_type(cursor.type):
                flag = True
        elif cursor.kind == CursorKind.PAREN_EXPR:
            # PAREN_EXPR: 括号表达式, 如 (fp), 内部可能是函数指针
            if _is_function_pointer_type(cursor.type):
                flag = True
        elif cursor.kind == CursorKind.CALL_EXPR:
            # CALL_EXPR: 光标落在调用表达式上, 如整个 foo()
            flag = True

        problem.is_false_alarm = flag
        return problem
