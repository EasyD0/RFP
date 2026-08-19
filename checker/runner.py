"""
checker.runner - 检查调度 (串行 / 多进程).

- total_check: 串行, 便于调试
- total_check_parallel: 多进程, worker 通过 initializer 复用反序列化的 CodeContext
"""

from multiprocessing import Pool, cpu_count
from typing import Iterable

from MyPyLib.LogSet import logSetup
from data_structure import Problem
from clangd_tool import kill_all_clangd_processes

from .base import Checker, CheckerDict
from .context import CodeContext

logger = logSetup(__name__)


def total_check(
    problems: Iterable[Problem], code_tool: CodeContext, rule_code_set: set[str] | None
):
    """
    串行检查可调试
    :param problems: 问题
    :param code_tool: 代码分析工具
    :param rule_code_set: 违反码集合
    :return:
    """
    all_check_type: set[type[Checker]] = set()
    if rule_code_set:
        for k in rule_code_set:
            all_check_type.update(CheckerDict.get(k, set()))
    else:
        for v in CheckerDict.values():
            all_check_type.update(v)

    def _check(
        problem: Problem, _code_tool: CodeContext, checker_types: set[type[Checker]]
    ):
        """
        对一个问题应用所有检查规则
        """
        for _tp in checker_types:
            _tp.do(problem, _code_tool)
            if problem.is_false_alarm:
                break
        return problem

    false_alarm_num = 0
    problem_num = 0
    for p in problems:
        _check(p, code_tool, all_check_type)
        false_alarm_num += p.is_false_alarm
        problem_num += 1
    print(f"误报率为{false_alarm_num/problem_num}")
    return problems


# %% 多进程并行计算
# CodeContext 整体可序列化: worker 通过 initializer 接收父进程中已构建好的
# 对象 (反序列化恢复, 不执行 __init__), 因此不会在子进程中重复初始化
# Preprocessor, 也不会重写 .resp / compile_commands.json, 没有文件竞态.

# 模块级全局变量, 供 worker 进程使用 (由 initializer 写入, 避免每个任务重复 pickle)
_g_code_tool: CodeContext | None = None
_g_checker_types: set[type[Checker]] = set()


def _init_worker(code_tool: CodeContext, checker_types: set[type[Checker]]):
    """
    在每个 worker 进程启动时调用一次. 这里拿到的是父进程 pickle 过来的
    CodeContext 实例: 反序列化恢复, 不会执行 __init__, 也就不会重复初始化
    Preprocessor 或写文件. 存入模块级全局变量, 避免为每个任务重复传输.
    """
    global _g_code_tool, _g_checker_types
    _g_code_tool = code_tool
    _g_checker_types = checker_types


def _worker_check(problem: Problem) -> Problem:
    """
    Worker 函数: 从模块级全局变量取 code_tool 和 checker_types,
    对单个 problem 串行应用所有检查规则.
    """
    global _g_code_tool, _g_checker_types
    for _tp in _g_checker_types:
        _tp.do(problem, _g_code_tool)
        if problem.is_false_alarm:
            break
    return problem


def total_check_parallel(
    problems: Iterable[Problem], code_tool: CodeContext, rule_code_set: set[str] | None
) -> list[Problem]:
    """
    多进程并发检查. 每个 problem 作为一个独立任务分发到 worker 进程.
    CodeContext 整体可序列化: 通过 initializer 把父进程中已构建好的实例
    传给每个 worker (反序列化恢复, 不重建对象), 并存入模块级全局变量,
    避免为每个任务重复 pickle 开销.
    """
    all_check_type: set[type[Checker]] = set()
    if rule_code_set:
        for k in rule_code_set:
            all_check_type.update(CheckerDict.get(k, set()))
    else:
        for v in CheckerDict.values():
            all_check_type.update(v)

    problem_list = list(problems)
    # chunksize 太小会导致 IPC 开销过大; 太大影响负载均衡
    chunksize = max(1, len(problem_list) // (cpu_count() * 4))

    with Pool(
        processes=max(1, int(cpu_count() / 1.2)),
        initializer=_init_worker,
        initargs=(code_tool, all_check_type),
    ) as pool:
        results = pool.map(_worker_check, problem_list, chunksize=chunksize)

    # Pool 退出后兜底清理: worker 进程被 terminate 时, 其内部 find_references
    # 启动的 clangd 子进程可能变孤儿, 这里统一杀掉
    kill_all_clangd_processes()

    false_alarm_num = sum(p.is_false_alarm for p in results)
    problem_num = len(results)
    if problem_num:
        print(f"误报率为{false_alarm_num / problem_num:.2%}")
    return results
