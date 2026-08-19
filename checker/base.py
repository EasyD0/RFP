"""
checker.base - 检查器框架 (基类 / 装饰器 / 注册表).

- Checker: 检查器基类, do() 按注册顺序执行子流程, 一旦判定误报立即短路
- register_checker / tag_padding / common_method / un_used: 注册与标记装饰器
- Checker_isUsed: 附加到每条规则的通用检查 (未参与编译文件 → 误报)
"""

from functools import wraps
from typing import Callable

from MyPyLib.LogSet import logSetup
from data_structure import Problem

from .context import CodeContext

logger = logSetup(__name__)
CheckerDict: dict[str, set[type["Checker"]]] = {}  # 检查器字典


class Checker:
    """
    检查器基类
    """

    rule_code: str = ""  # 将由装饰器注入
    # 所有方法
    _ProcessListAll: list[Callable[[Problem, CodeContext], Problem]] = []
    # 通用的方法
    _ProcessListCommon: list[Callable[[Problem, CodeContext], Problem]] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        for base in cls.__bases__:
            if base is Checker:
                continue

            if issubclass(base, Checker):
                raise TypeError(
                    f"非法继承！类 '{cls.__name__}' 不能继承自 '{base.__name__}'。\n"
                    f"原因：'{base.__name__}' 已经是 Checker 的子类，Checker 体系禁止多层继承。"
                )

    @classmethod
    def do(
        cls, problem: Problem, code_tool: CodeContext, only_common_check=False
    ) -> Problem:
        """
        :param problem:
        :param code_tool:
        :param only_common_check: 只做通用检查
        :return:
        """
        if problem.rule_code.replace(" ", "") != cls.rule_code:
            logger.debug("问题不属于规则{}, 直接跳过".format(cls.rule_code))
            return problem
        if not problem.code_line:
            logger.debug("没有代码行内容, 直接跳过")
            return problem

        if only_common_check:
            member_funcs = cls._ProcessListCommon
        else:
            member_funcs = cls._ProcessListAll

        for f in member_funcs:
            problem = f(problem, code_tool)
            if problem.is_false_alarm:
                break
        if not problem.is_false_alarm:
            logger.debug("问题不识别为误报")
        else:
            logger.debug("问题识别为误报")
        return problem


def register_checker(
    rule_code: str, rule_name: str = "", doc: str = ""
) -> Callable[[type[Checker]], type[Checker]]:
    def _decorator(class_type: type[Checker]) -> type[Checker]:
        CheckerDict.setdefault(rule_code, set()).add(class_type)
        class_type.rule_code = rule_code
        class_type.__doc__ = (
            f"{rule_code}-{rule_name}-{doc}" + class_type.__doc__
            if class_type.__doc__
            else ""
        )

        # 处理类型的ProcessList
        process_list = []
        common_process_list = []
        for attr_name in dir(class_type):
            class_attr = getattr(class_type, attr_name)
            if callable(class_attr):
                if hasattr(class_attr, "__un_used__"):
                    # 暂不使用的方法
                    continue
                if hasattr(class_attr, "__is_process__"):
                    process_list.append(class_attr)
                if hasattr(class_attr, "__is_common__"):
                    common_process_list.append(class_attr)

        class_type._ProcessListAll = sorted(process_list, key=lambda f: f.__name__)
        class_type._ProcessListCommon = sorted(
            common_process_list, key=lambda f: f.__name__
        )
        return class_type

    return _decorator


def tag_padding(tag: str) -> Callable:

    if not tag.endswith(">"):
        tag += ">"
    if not tag.startswith("<"):
        tag = "<" + tag

    def _decorator(func: Callable[[Problem, CodeContext], Problem]):
        @wraps(func)
        def new_func(*args, **kwargs):
            res: Problem = func(*args, **kwargs)
            if res.is_false_alarm:
                res.pro_des = res.pro_des + tag
                logger.error("识别为: {}".format(tag))
            return res

        new_func.__is_process__ = True
        new_func.tag = tag  # 用于后面根据配置文件来开启和关闭子算法
        return new_func

    return _decorator


def common_method(
    func: Callable[[Problem, CodeContext], Problem],
) -> Callable[[Problem, CodeContext], Problem]:
    """将子算法标记为通用的"""

    func.__is_common__ = True
    return func


def un_used() -> Callable[[Callable], Callable]:
    """将子算法标记为不使用的"""

    def _decorator(func: Callable[[Problem, CodeContext], Problem]):
        func.__un_used__ = True
        return func

    return _decorator


class Checker_isUsed:
    @tag_padding("<未参与编译文件>")
    @staticmethod
    def do(problem: Problem, code_tool: CodeContext, only_common_check=True) -> Problem:
        if only_common_check:
            return problem

        if not problem.file_name:
            logger.debug("无文件名, 不适用")
            return problem

        for file in code_tool.all_used_files:
            if file.name.lower() == problem.file_name.lower():
                break
        else:
            problem.set_false()
        return problem
