import re
from functools import wraps
from pathlib import Path
from typing import Callable, Iterable

from MyPyLib.LogSet import logSetUp
from MyPyLib.Preprocessor import Preprocessor
from clang.cindex import Cursor, CursorKind, StorageClass, LinkageKind

from clang_tool import find_colum, get_cursor_in_pos
from data_structure import Problem
from clangd_tool import (
    find_references,
    Clangd_EXE,
    get_ref_code,
    kill_all_clangd_processes,
)

logger = logSetUp(__name__)
CheckerDict: dict[str, set[type["Checker"]]] = {}  # 检查器字典


class CodeContext:
    """
    代码处理工具

    注意: 内部持有的 Preprocessor / libclang / clangd 子进程等资源
    无法跨进程序列化, 因此通过 __getstate__/__setstate__ 只 pickle
    构造参数 (proj_dir, proj_name, chip_name), 在 worker 进程反序列化
    时重新构造, 让本对象可安全用于 multiprocessing.
    """

    def __init__(self, proj_dir: Path, proj_name: str = "", chip_name: str = ""):
        self.proj_dir = proj_dir
        self.proj_name = proj_name
        self.chip_name = chip_name
        self.por = Preprocessor(
            proj_dir,
            response_dir=Path("./.resp"),
            proj_name=proj_name,
            chip_name=chip_name,
        )
        self.all_used_files = self.por.getUsedFiles()

    def __getstate__(self):
        # 只序列化构造参数, 丢弃 por / all_used_files 等不可序列化资源
        return {
            "proj_dir": self.proj_dir,
            "proj_name": self.proj_name,
            "chip_name": self.chip_name,
        }

    def __setstate__(self, state):
        # 在 worker 进程中用构造参数重新初始化, 重建 por / all_used_files
        self.__init__(
            proj_dir=state["proj_dir"],
            proj_name=state["proj_name"],
            chip_name=state["chip_name"],
        )

    def get_args(self, file: Path):
        """
        路径可以是相对/绝对路径
        :param file:
        :return:
        """
        return self.por.get_args(file)


class Checker:
    """
    检查器基类
    """

    rule_code: str = ""  # 将由装饰器注入
    ProcessList: list[Callable[[Problem, CodeContext], Problem]] = []

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
    def do(cls, problem: Problem, code_tool: CodeContext) -> Problem:
        if problem.rule_code.replace(" ", "") != cls.rule_code:
            logger.debug("问题不属于规则{}, 直接跳过".format(cls.rule_code))
            return problem
        if not problem.code_line:
            logger.debug("没有代码行内容, 直接跳过")
            return problem

        for f in cls.ProcessList:
            problem = f(problem, code_tool)
            if problem.is_false_alarm:
                break

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
        for attr_name in dir(class_type):
            attr = getattr(class_type, attr_name)
            if callable(attr) and hasattr(attr, "__is_process__"):
                process_list.append(attr)
        class_type.ProcessList = sorted(process_list, key=lambda f: f.__name__)

        return class_type

    return _decorator


def tag_padding(tag: str) -> Callable:
    def _decorator(func: Callable[[Problem, CodeContext], Problem]):
        @wraps(func)
        def new_func(*args, **kwargs):
            res: Problem = func(*args, **kwargs)
            if res.is_false_alarm:
                res.pro_des = res.pro_des + tag
                logger.error("识别为: {}".format(tag))
            return res

        new_func.__is_process__ = True
        return new_func

    return _decorator


class Checker_isUsed:
    @tag_padding("<未参与编译文件>")
    @staticmethod
    def do(problem: Problem, code_tool: CodeContext) -> Problem:
        if not problem.file_name:
            logger.debug("无文件名, 不适用")
            return problem

        for file in code_tool.all_used_files:
            if file.name.lower() == problem.file_name.lower():
                break
        else:
            problem.set_false()
        return problem


@register_checker("69D", "变量未赋值就使用")
class Checker_69D(Checker):
    @tag_padding("<已在声明处显式初始化>")
    @staticmethod
    def func1(problem: Problem, code_tool: CodeContext) -> Problem:
        if "=" in problem.code_line and not "==" in problem.code_line:
            problem.set_false()
        return problem

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
        # TODO
        return problem
        raise NotImplementedError
        # 其他情况 需要追踪初始化语句位置


@register_checker("57S", "无作用的语句")
class Checker_57S(Checker):

    @tag_padding("<未用的参数>")
    @staticmethod
    def func0(problem: Problem, code_tool: CodeContext) -> Problem:
        first_line = problem.code_line[0].token.upper()
        if re.search(r"UNUSED_PARAMETER *\(", first_line):
            problem.set_false()
        return problem

    @tag_padding("<日志打印>")
    @staticmethod
    def func1(problem: Problem, code_tool: CodeContext) -> Problem:
        first_line = problem.code_line[0].token.lower()
        first_line = first_line.split(r"(")[0]
        if "log" in first_line or "print" in first_line:
            problem.set_false()
        return problem

    @tag_padding("<置为void>")
    @staticmethod
    def func1(problem: Problem, code_tool: CodeContext) -> Problem:
        first_line = problem.code_line[0]
        if re.search(r"\( *void *\)", first_line.token):
            problem.set_false()
        return problem

    @tag_padding("<声明语句>")
    @staticmethod
    def func2(problem: Problem, code_tool: CodeContext) -> Problem:
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
    def func3(problem: Problem, code_tool: CodeContext) -> Problem:
        args = code_tool.get_args(problem.file_path1(code_tool.proj_dir))
        cursor: Cursor | None = get_cursor_in_pos(
            problem.code_line[0], code_tool.proj_dir, args
        )
        if not cursor:
            logger.warning("无法解析")
            return problem

        flag = False
        # 如果本身就是函数/函数指针的调用
        if cursor.kind == CursorKind.DECL_REF_EXPR:
            if cursor.get_definition().kind == CursorKind.FUNCTION_DECL:
                # 函数类型
                flag = True
            elif "(*)" in cursor.type.get_canonical().spelling:
                # 函数指针
                flag = True
            elif True:
                # TODO 还会有什么情况?
                raise NotImplementedError

        # 如果是括号表达式 CursorKind.PAREN_EXPR, 并且内部是一个函数指针
        elif cursor.kind == CursorKind.PAREN_EXPR:
            if "(*)" in cursor.type.get_canonical().spelling:
                flag = True
        else:
            # TODO 还会有什么情况?
            raise NotImplementedError

        problem.is_false_alarm = flag
        return problem


# 暂时OK
@register_checker("1X", "在整个系统中声明的类型不一致")
class Checker_1X(Checker):
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

    @tag_padding("<两个声明类型和链接,存储属性均一致>")
    @staticmethod
    def func2(problem: Problem, code_tool: CodeContext) -> Problem:
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
            logger.debug("<非误报: 具有冲突的类型>")
            problem.pro_des += "<非误报: 具有冲突的类型>"
            problem.clear_false()
            return problem

        problem.set_false()
        return problem


@register_checker("47", "数组越界")
class Checker_47S(Checker):
    @tag_padding("<数组长度检查确保不越界>")
    @staticmethod
    def func1(problem: Problem, code_tool: CodeContext) -> Problem:
        """
        只做简单的单层检查, 不考虑复杂的数据流逻辑
        获取数组节点, 确定数组长度
        获取语句节点A,
        遍历函数树, 重定位到节点A, 在遍历的路径中记录到A的父信息, 检查父节点中是否存在下标约束,
        然后检查该约束是否可使得数组不越界
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

        arr_name = problem.rule_name.token.split("[*]")[0]
        if not re.search(r"\[|\.|->", arr_name):
            arr_name = arr_name.strip()
        elif not "[" in arr_name:
            arr_name = re.split(r"->|\.", arr_name)[-1]
        else:
            arr_name = None
        if not arr_name:
            logger.warning("无法识别数组名称")
            return problem

        logger.debug(f"数组名称为 {arr_name}")

        clangd_args = code_tool.get_args(problem.code_line[0].path)

        # 文本检查是否存在多种数组下标的访问, 比如这行文本种出现了 arr[i]+arr[i+1],  如果是则直接退出
        def extract_subscripts(var_name, expression) -> set[str]:
            """
            从表达式中提取指定变量名的所有下标内容。
            对于这种情况会多找 arr[i+1] + arr[f(x, y)] + a.arr[g]
            """
            results = set()
            n = len(expression)
            v_len = len(var_name)
            i = 0

            while i < n:
                # 1. 检查当前位置是否匹配变量名
                if expression[i : i + v_len] == var_name:
                    # 2. 检查变量名前的边界 (确保不是更长标识符的一部分，如 barr)
                    # 如果 i>0 且前一个字符是字母、数字或下划线，则不是独立变量
                    if i > 0 and (
                        expression[i - 1].isalnum() or expression[i - 1] == "_"
                    ):
                        i += 1
                        continue

                    # 3. 检查变量名后是否紧跟 '['
                    next_char_idx = i + v_len
                    if next_char_idx < n and expression[next_char_idx] == "[":
                        # 4. 开始寻找匹配的右括号 ']'
                        start_bracket = next_char_idx
                        balance = 1  # 已经遇到一个 '['
                        j = start_bracket + 1

                        while j < n and balance > 0:
                            if expression[j] == "[":
                                balance += 1
                            elif expression[j] == "]":
                                balance -= 1
                            j += 1

                        # 如果找到了匹配的右括号 (balance 归零)
                        if balance == 0:
                            # 提取括号内的内容
                            content = expression[start_bracket + 1 : j - 1]
                            results.add(content)

                        # 跳过已处理的部分
                        i = j
                        continue

                i += 1

            return results

        all_subscript_text = extract_subscripts(arr_name, problem.code_line[0].token)
        if len(all_subscript_text) != 1:
            logger.warning("下标情况复杂, 不再检查直接退出")
            return problem
        elif re.search(r"\W", list(all_subscript_text)[0].strip()):
            logger.warning("下标情况复杂, 不再检查直接退出")
            return problem

        idx_name: str = list(all_subscript_text)[0].strip()

        # 数组下标的cursor
        idx_cursor = get_cursor_in_pos(
            problem.code_line[0], code_tool.proj_dir, clangd_args, idx_name
        )

        # 数组变量的cursor
        arr_cursor = get_cursor_in_pos(
            problem.code_line[0], code_tool.proj_dir, clangd_args, arr_name
        )

        # 语句的cursor
        statement_cursor = get_cursor_in_pos(
            problem.code_line[0], code_tool.proj_dir, args=clangd_args
        )
        if arr_cursor.kind != CursorKind.DECL_REF_EXPR:
            logger.warning("数组节点查找错误")
            return problem

        # 求解数组长度
        arr_size: int = arr_cursor.type.get_canonical().get_array_size()
        if not isinstance(arr_size, int) or arr_size < 1:
            logger.error("数组长度获取失败")
            return problem

        if "literal" in str(idx_cursor.kind).lower():
            if not idx_cursor.spelling:
                logger.error("字面量的数组下标值未知")
                return problem
            else:
                try:
                    if int(idx_cursor.spelling) >= arr_size:
                        problem.set_false()
                        return problem
                except Exception:
                    logger.warning("无法处理数字下标的字面量")
                    return problem
        elif idx_cursor.kind != CursorKind.DECL_REF_EXPR:
            # 检查数组下标是否为变量
            # 从函数定义节点开始查询到这个节点为止, 并尝试发现
            pass
        elif idx_cursor.get_definition().kind == CursorKind.ENUM_CONSTANT_DECL:
            # 是枚举引用
            idx_value = idx_cursor.get_definition().enum_value
            if idx_value >= arr_size:
                problem.set_false()
                return problem
        else:
            # 不是枚举引用的通常变量
            # 需要追踪其变量

            raise NotImplementedError
        return problem


def is_unused_return_call_start(ref_code: str, func_name: str) -> bool:
    """
    判断 ref_code 是否以如下两种形式之一开头 (允许前后及单词之间存在任意空白字符):

        1. (void) func_name(    —— 显式丢弃返回值的函数调用
        2. func_name(           —— 直接的函数调用

    用 ^ 锚定开头, 严格判断 "以 ... 开头", 而非出现在任意位置;
    (?:...)? 使 (void) 部分可选, 一条正则同时覆盖两种形式;
    func_name 经 re.escape 处理, 避免其中含有正则元字符时误匹配;
    末尾要求 ( , 既确认是函数调用形式, 也避免匹配到同前缀的其它标识符
    (例如 func_name_ext 不会被当作 func_name 命中)。

    :param ref_code: 引用处的代码片段
    :param func_name: 被调用的函数名
    :return: 命中返回 True, 否则 False
    """
    escaped = re.escape(func_name)
    pattern = rf"^\s*(?:\(\s*void\s*\)\s*)?{escaped}\s*\("
    return re.match(pattern, ref_code) is not None


@register_checker("36S", "函数没有返回语句")
class CHecker_36S(Checker):
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
        # 被引用的函数名已经解析了, TODO 这里要检查下这个规则是否有func_name
        func_name = problem.func_name
        # 定位所有引用的代码

        # 定位所有引用的代码
        ref_code_locaitons = find_references(
            Clangd_EXE,
            code_tool.proj_dir,
            problem.code_line[0].path,
            problem.code_line[0].line - 1,
            find_colum(problem.code_line[0], func_name, code_tool.proj_dir - 1),
        )

        # 从定位的引用位置中提取代码
        ref_codes = get_ref_code(ref_code_locaitons)

        for ref_code in ref_codes:
            if "=" in ref_code:
                problem.clear_false()
                problem.pro_des += "存在一处赋值发生, 应该不是误报"
                return problem
            elif is_unused_return_call_start(ref_code, func_name):
                continue
            else:
                # 所有的引用都是直接使用函数, 这种情况是误报
                problem.set_false()
            return problem
        return problem

for k, v in CheckerDict.items():
    CheckerDict[k].add(Checker_isUsed)

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


# %% 多进程并行计算, 需要考虑进程同步和序列化问题
from multiprocessing import Pool, cpu_count

# 模块级全局变量, 供 worker 进程使用 (由 initializer 写入, 避免 pickle 传 code_tool)
_g_code_tool: CodeContext | None = None
_g_checker_types: set[type[Checker]] = set()


def _init_worker(code_tool: CodeContext, checker_types: set[type[Checker]]):
    """
    在每个 worker 进程启动时调用一次, 把 code_tool 和 checker_types
    写入模块级全局变量, 避免被每个任务重复 pickle.
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
    通过 initializer 让每个 worker 进程只初始化一次 code_tool,
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
