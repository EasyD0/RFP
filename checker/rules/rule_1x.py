"""
checker.rules.rule_1x - 规则 1X: 在整个系统中声明的类型不一致.
"""

from clang.cindex import LinkageKind, StorageClass
from MyPyLib.LogSet import logSetup
from data_structure import Problem

from ..base import Checker, common_method, register_checker, tag_padding
from ..context import CodeContext
from clang_tool import get_cursor_in_pos

logger = logSetup(__name__)


# 暂时OK
@register_checker("1X", "在整个系统中声明的类型不一致")
class Checker_1X(Checker):
    @common_method
    @tag_padding("<代码行没有两处>")
    @staticmethod
    def func1(problem: Problem, code_tool: CodeContext) -> Problem:
        if len(problem.code_line) != 2:
            problem.set_false()
        return problem

    # @tag_padding("<两个声明文本一致>")
    # @staticmethod
    # def func2(problem: Problem, code_tool: CodeContext) -> Problem:
    #     if problem.code_line[0].token == problem.code_line[1].token:
    #         problem.set_false()
    #     return problem

    @tag_padding("<两个声明的类型、链接、存储属性均一致>")
    @staticmethod
    def func2(problem: Problem, code_tool: CodeContext) -> Problem:
        """
        检查两个声明的类型、链接、存储属性
        :param problem:
        :param code_tool:
        :return:
        """
        clangd_args = code_tool.get_args(code_tool.proj_dir / problem.code_line[0].path)
        cursor0 = get_cursor_in_pos(
            problem.code_line[0], code_tool.proj_dir, clangd_args
        )
        clangd_args = code_tool.get_args(code_tool.proj_dir / problem.code_line[1].path)
        cursor1 = get_cursor_in_pos(
            problem.code_line[1], code_tool.proj_dir, clangd_args
        )

        storage_class0 = cursor0.storage_class
        storage_class1 = cursor1.storage_class

        # 检查是否有冲突的存储类型
        if storage_class1 != storage_class0:
            if StorageClass.STATIC in {
                storage_class1,
                storage_class0,
            } or StorageClass.REGISTER in {storage_class1, storage_class0}:
                logger.debug("<非误报: 具有冲突的存储属性>")
                problem.pro_des += "<非误报: 具有冲突的存储属性>"
                problem.clear_false()
                return problem

        linkage0 = cursor0.linkage
        linkage1 = cursor1.linkage

        if linkage0 != linkage1:
            if LinkageKind.INTERNAL in {
                linkage0,
                linkage1,
            }:
                logger.debug("<非误报: 具有冲突的链接属性>")
                problem.pro_des += "<非误报: 具有冲突的链接属性>"
                problem.clear_false()
                return problem

        type0 = cursor0.type.get_canonical()
        type1 = cursor1.type.get_canonical()
        if type1.spelling != type0.spelling:
            # 将 uint8_t 和 bool 视为同一类型
            ts0 = type0.spelling.replace("uint8_t", "bool")
            ts1 = type1.spelling.replace("uint8_t", "bool")
            if ts0 != ts1:
                logger.debug("<非误报: 具有冲突的类型>")
                problem.pro_des += "<非误报: 具有冲突的类型>"
                problem.clear_false()
                return problem
            else:
                logger.debug("bool/uint8_t")
        problem.set_false()
        return problem
