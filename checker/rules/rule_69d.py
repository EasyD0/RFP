"""
checker.rules.rule_69d - 规则 69D: 变量未赋值就使用.
"""

import re

from clang.cindex import Cursor, CursorKind, StorageClass
from MyPyLib.LogSet import logSetup
from data_structure import Problem

from ..base import Checker, common_method, register_checker, tag_padding
from ..context import CodeContext
from clang_tool import (
    get_cursor_in_func,
    get_cursor_in_pos,
    get_cursor_text,
    get_innermost_block,
    get_same_level_nodes,
)

logger = logSetup(__name__)


@register_checker("69D", "变量未赋值就使用")
class Checker_69D(Checker):
    @common_method
    @tag_padding("<代码行没有两处>")
    @staticmethod
    def func0(problem: Problem, code_tool: CodeContext) -> Problem:
        if len(problem.code_line) != 2:
            problem.set_false()
        return problem

    @common_method
    @tag_padding("<已在声明处显式初始化>")
    @staticmethod
    def func1(problem: Problem, code_tool: CodeContext) -> Problem:
        if "=" in problem.code_line and not "==" in problem.code_line:
            problem.set_false()
        return problem

    @common_method
    @tag_padding("<全局变量自动零初始化>")
    @staticmethod
    def func2(problem: Problem, code_tool: CodeContext) -> Problem:
        if not problem.func_name or "Whole program" in problem.func_name:
            problem.set_false()
        return problem

    @tag_padding("<静态变量自动零初始化>")
    @staticmethod
    def func3(problem: Problem, code_tool: CodeContext) -> Problem:
        # if "static " in problem.code_line[0].token:
        if re.search(r"\sstatic\s", problem.code_line[0].token):
            problem.set_false()
            return problem

        # 通过游标检查 是否为静态变量, 因为有些不用static声明, 而是使用宏 (比如 STATIC) 等
        clangd_args = code_tool.get_args(problem.file_path1(code_tool.proj_dir))
        cursor = get_cursor_in_pos(
            problem.code_line[0], code_tool.proj_dir, clangd_args
        )
        if not (cursor and cursor.kind in {CursorKind.VAR_DECL, CursorKind.TYPE_REF}):
            logger.warning("发生意外跳过检查")
            return problem

        if cursor.kind == CursorKind.TYPE_REF:
            # 此时必然不是静态的, 因为这一行开头就是类型引用
            return problem

        if cursor.storage_class == StorageClass.STATIC:
            problem.set_false()
            return problem

        return problem
        # TODO 这里还要加什么步骤?
        raise NotImplementedError

    @tag_padding("<中途发现初始化语句>")
    @staticmethod
    def func4(problem: Problem, code_tool: CodeContext) -> Problem:
        """
        一个违规例子形如
            APP/PSC/src/PwrSrcDiagnose.c:1024
            PscTimeInfo stTimeInfo;

            APP/PSC/src/PwrSrcDiagnose.c:1025
            unErr[0] = PscSigMgrGetSig(Date_Information_Month_2B6_S, &stTimeInfo.m_unMonth, NULL, NULL);

            UR anomaly, 变量未赋值就使用 : stTimeInfo.m_unMin

        退行实现: 只检查引用语句所在语句块中, 同一层级且起始位置不晚于引用位置的
        语句, 是否为变量提供了直接初始化/取地址行为 (如 var = ..., &var / &var.member
        传入函数), 不检查嵌套分支、间接赋值等行为 (比如通过指针赋值)。
        找不到证据时保持原问题不变 (不标记误报), 不抛异常。

        :param problem:
        :param code_tool:
        :return:
        """
        if len(problem.code_line) < 2:
            logger.debug("代码行不足两处, 不适用")
            return problem

        # 报告中的变量名可能是成员表达式, 如 stTimeInfo.m_unMin,
        # 需要提取根变量名 stTimeInfo, 用于匹配 &stTimeInfo.m_unMonth 这类写法
        var_name: str = problem.rule_name.token.strip()
        if not var_name:
            logger.debug("无变量名, 不适用")
            return problem
        root_match = re.match(r"[A-Za-z_]\w*", var_name)
        root_var = root_match.group(0) if root_match else var_name

        # 使用处所在文件上解析游标
        clangd_args = code_tool.get_args(problem.file_path2(code_tool.proj_dir))
        ref_cursor: Cursor | None = get_cursor_in_pos(
            problem.code_line[1],
            code_tool.proj_dir,
            clangd_args,
            problem.rule_name.token,
        )
        if not ref_cursor:
            logger.warning("无法定位使用处游标, 跳过")
            return problem

        def is_skip_kind(k: CursorKind) -> bool:
            # 无需检查的 CursorKind: 分支/循环/声明/跳转等结构
            # 不提供可靠的初始化证据, 直接跳过
            skip_kinds = {
                CursorKind.COMPOUND_STMT,  # 复合语句 {}
                CursorKind.DECL_STMT,  # 声明语句
                CursorKind.RETURN_STMT,  # return语句
                CursorKind.IF_STMT,  # if语句
                CursorKind.FOR_STMT,  # for语句
                CursorKind.WHILE_STMT,  # while语句
                CursorKind.DO_STMT,  # do-while语句
                CursorKind.SWITCH_STMT,  # switch语句
                CursorKind.CASE_STMT,  # case语句
                CursorKind.DEFAULT_STMT,  # default语句
                CursorKind.BREAK_STMT,  # break语句
                CursorKind.CONTINUE_STMT,  # continue语句
                CursorKind.GOTO_STMT,  # goto语句
                CursorKind.LABEL_STMT,  # 标签语句
                CursorKind.NULL_STMT,  # 空语句
            }
            return k in skip_kinds

        def is_init_text(text: str) -> bool:
            # 判断语句文本是否对变量有直接初始化/取地址行为
            # 先移除行注释, 避免注释里的文本被误判
            code = text.split("//", 1)[0]
            if not code.strip():
                return False

            patterns = {
                rf"\b{re.escape(root_var)}\s*=(?!=)",  # 匹配 stTimeInfo = {0}; 排除 == 的情况
                rf"&\s*{re.escape(root_var)}\b",  # &stTimeInfo / &stTimeInfo.member
                rf"\b{re.escape(var_name)}\s*=(?!=)",  # stTimeInfo.m_unMin = 1; 排除 == 的情况
                rf"&\s*{re.escape(var_name)}(?![\w.])",  # &stTimeInfo.m_unMin
            }
            return any(re.search(p, code) for p in patterns)

        func_def = get_cursor_in_func(ref_cursor)
        if not func_def:
            logger.warning("未找到所在函数定义, 跳过")
            return problem

        block = get_innermost_block(ref_cursor)
        if not block:
            logger.warning("未找到包含使用处的语句块, 跳过")
            return problem

        candidate_cursor = get_same_level_nodes(ref_cursor)
        for candidate in candidate_cursor:
            if is_skip_kind(candidate.kind):
                continue

            if is_init_text(get_cursor_text(candidate)):
                problem.set_false()
                return problem

        logger.debug("同一层级未发现对 {} 的初始化语句".format(var_name))
        return problem

        # TODO 需要测试下这种情况
        #   {
        #       int x;
        #       int a;
        #       int *b=&a;
        #       if (x>0) a=1;
        #       x=a; //最后的a是否违规?
        #   }
        #   有可能 int *b=&a 会导致以为初始化, 或者  a=1; 可能被视为同一层的

    @common_method
    @tag_padding("<此处没有直接使用变量值, 而是使用其地址>")
    @staticmethod
    def func5(problem: Problem, code_tool: CodeContext) -> Problem:
        """
        一个违规例子形如
            APP/PSC/src/PwrSrcDiagnose.c:1024
            PscTimeInfo stTimeInfo;

            APP/PSC/src/PwrSrcDiagnose.c:1025
            unErr[0] = PscSigMgrGetSig(Date_Information_Month_2B6_S, &stTimeInfo.m_unMonth, NULL, NULL);
        在1025使用处实际上正是要对stTimeInfo.m_unMonth进行赋值, 这种情况需要视为误报
        :param problem:
        :param code_tool:
        :return:
        """
        var_name = problem.rule_name.token.strip()
        use_token = problem.code_line[1].token.strip()
        if re.search(rf"&\s+{var_name}", use_token):
            # 发现对它的引用
            problem.set_false()

        return problem
