import re
from functools import wraps, partial
from pathlib import Path
from typing import Callable, Iterable

from MyPyLib.LogSet import logSetup
from MyPyLib.Preprocessor import Preprocessor
from clang.cindex import Cursor, CursorKind, StorageClass, LinkageKind, File, Type

from clang_tool import get_cursor_in_pos, get_cursor_at_line
from data_structure import Problem, CodePos
from clangd_tool import (
    find_references,
    kill_all_clangd_processes,
)

logger = logSetup(__name__)
CheckerDict: dict[str, set[type["Checker"]]] = {}  # 检查器字典


# TODO 若问题被clear_false() 它接下来不应该继续执行检查了
#  problem的排序函数 可能需要重新编写
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

    def get_command_json(self, source_file):
        raise NotImplementedError
        return self.por.get_command_json(source_file)


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
        """
        一个违规例子形如
            APP/PSC/src/PwrSrcDiagnose.c:1024
            PscTimeInfo stTimeInfo;

            APP/PSC/src/PwrSrcDiagnose.c:1025
            unErr[0] = PscSigMgrGetSig(Date_Information_Month_2B6_S, &stTimeInfo.m_unMonth, NULL, NULL);//0x2B6, 2.0-2.7, 日期信息：月

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

        # 使用处所在文件上解析游标 (原实现解析了声明处游标却从未使用)
        clangd_args = code_tool.get_args(problem.file_path2(code_tool.proj_dir))
        ref_cursor: Cursor | None = get_cursor_in_pos(
            problem.code_line[1], code_tool.proj_dir, clangd_args
        )
        if not ref_cursor:
            logger.warning("无法定位使用处游标, 跳过")
            return problem

        def loc_key(loc) -> tuple[int, int]:
            # SourceLocation 的排序键, 用于比较源码位置
            return (loc.line, loc.column)

        def contains(node: Cursor, target: Cursor) -> bool:
            # 判断 node 的源码范围是否包含 target 的位置
            if node.extent.start.file != target.extent.start.file:
                return False
            return (
                loc_key(node.extent.start)
                <= loc_key(target.extent.start)
                <= loc_key(node.extent.end)
            )

        def get_func_def(node: Cursor) -> Cursor | None:
            # 从整个翻译单元中找到包含 node 的函数定义
            tu = node.translation_unit
            if not tu:
                return None

            def _find(root: Cursor) -> Cursor | None:
                if root.kind == CursorKind.FUNCTION_DECL and contains(root, node):
                    return root
                for child in root.get_children():
                    res = _find(child)
                    if res:
                        return res
                return None

            return _find(tu.cursor)

        def get_innermost_block(root: Cursor, node: Cursor) -> Cursor | None:
            # 找到包含 node 的最内层复合语句块, 即 node 所在层级的语句块
            best: Cursor | None = None
            best_size: tuple[int, int] | None = None

            def _walk(cur: Cursor):
                nonlocal best, best_size
                if cur.kind == CursorKind.COMPOUND_STMT and contains(cur, node):
                    size = (
                        cur.extent.end.line - cur.extent.start.line,
                        cur.extent.end.column - cur.extent.start.column,
                    )
                    if best is None or size < best_size:
                        best, best_size = cur, size
                for child in cur.get_children():
                    _walk(child)

            _walk(root)
            return best

        def get_same_level_nodes(block: Cursor, node: Cursor) -> list[Cursor]:
            # 收集与引用语句同一层级、且起始位置不晚于引用位置的兄弟语句
            pos = loc_key(node.extent.start)
            result = []
            for child in block.get_children():
                if child.extent.start.file != node.extent.start.file:
                    continue
                if loc_key(child.extent.start) <= pos:
                    result.append(child)
            return result

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

        def get_cursor_text(cur: Cursor) -> str:
            # 读取游标覆盖的完整源码文本 (可能跨多行), 供文本匹配使用
            src_file = cur.extent.start.file
            if not src_file:
                return ""
            with open(
                Path(src_file.name), "r", encoding="utf-8", errors="replace"
            ) as f:
                lines = f.readlines()
            start_line = cur.extent.start.line
            end_line = cur.extent.end.line
            return "".join(lines[start_line - 1 : end_line])

        def is_init_text(text: str) -> bool:
            # 判断语句文本是否对变量有直接初始化/取地址行为
            # 先移除行注释, 避免注释里的文本被误判
            code = text.split("//", 1)[0]
            if not code.strip():
                return False

            patterns = {
                rf"\b{re.escape(root_var)}\s*=(?!=)",  # stTimeInfo = {0}; (排除 ==)
                rf"&\s*{re.escape(root_var)}\b",  # &stTimeInfo / &stTimeInfo.member
                rf"\b{re.escape(var_name)}\s*=(?!=)",  # stTimeInfo.m_unMin = 1; (排除 ==)
                rf"&\s*{re.escape(var_name)}(?![\w.])",  # &stTimeInfo.m_unMin
            }
            return any(re.search(p, code) for p in patterns)

        func_def = get_func_def(ref_cursor)
        if not func_def:
            logger.warning("未找到所在函数定义, 跳过")
            return problem

        block = get_innermost_block(func_def, ref_cursor)
        if not block:
            logger.warning("未找到包含使用处的语句块, 跳过")
            return problem

        candidate_cursor = get_same_level_nodes(block, ref_cursor)
        for candidate in candidate_cursor:
            if is_skip_kind(candidate.kind):
                continue

            if is_init_text(get_cursor_text(candidate)):
                problem.set_false()
                return problem

        logger.debug("同一层级未发现对 {} 的初始化语句".format(var_name))
        return problem


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
    def func2(problem: Problem, code_tool: CodeContext) -> Problem:
        first_line = problem.code_line[0]
        if re.search(r"\( *void *\)", first_line.token):
            problem.set_false()
        return problem

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
        token: str = problem.code_line[0].token
        source_file: Path = problem.file_path1(code_tool.proj_dir)
        line_number = problem.code_line[0].line

        with open(source_file, "r", encoding="utf-8", ignores="replace") as f:
            all_lines = f.readlines()

        i = line_number - 1
        while 0 <= i < len(all_lines):
            cur_code = all_lines[i]
            if "{" in cur_code:
                break
            else:
                i -= 1
        if (i - 1 >= 0) and (
            "#pragma inline_asm" in all_lines[i - 1] or "__asm" in all_lines[i - 1]
        ):
            logger.debug("识别为汇编语言")
            problem.set_false()
        else:
            func_name = problem.func_name
            for l in all_lines:
                if re.search(rf"\n\s+#pragma inline_asm\s+{func_name}", l):
                    logger.debug("识别为汇编语言")
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
        # DECL_REF_EXPR: 直接引用函数名或函数指针变量, 如 foo 或 fp
        if cursor.kind == CursorKind.DECL_REF_EXPR:
            definition = cursor.get_definition()
            if definition is not None and definition.kind == CursorKind.FUNCTION_DECL:
                flag = True
            elif _is_function_pointer_type(cursor.type):
                flag = True

        # PAREN_EXPR: 括号表达式, 如 (fp), 内部可能是函数指针
        elif cursor.kind == CursorKind.PAREN_EXPR:
            if _is_function_pointer_type(cursor.type):
                flag = True

        # CALL_EXPR: 光标落在调用表达式上, 如整个 foo()
        elif cursor.kind == CursorKind.CALL_EXPR:
            flag = True

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

    @tag_padding("<数组长度检查确保不越界>")
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

        # 在代码行中定位 "arr[idx]" 区域, 避免 idx/arr 在行内更早的位置先出现
        # (比如 i = arr[i] 或 foo(0, arr[1])) 导致取到错误的游标
        access_match = re.search(
            re.escape(arr_name) + r"\[\s*(" + re.escape(idx_name) + r")\s*\]",
            problem.code_line[0].token,
        )
        if not access_match:
            logger.warning("无法在代码行中定位数组访问")
            return problem

        src_path = code_tool.proj_dir / problem.code_line[0].path
        # 数组名起始列 (1-based)
        arr_col = access_match.start() + 1
        # 下标内容起始列 (1-based)
        idx_col = access_match.start(1) + 1

        # 数组下标的cursor
        idx_cursor = get_cursor_at_line(
            src_path, problem.code_line[0].line, idx_col, clangd_args
        )

        # 数组变量的cursor
        arr_cursor = get_cursor_at_line(
            src_path, problem.code_line[0].line, arr_col, clangd_args
        )

        # 语句的cursor
        statement_cursor = get_cursor_at_line(
            src_path, problem.code_line[0].line, 1, clangd_args
        )

        if not idx_cursor or not arr_cursor:
            logger.warning("数组/下标游标获取失败")
            return problem

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

        if "literal" in str(idx_cursor.kind).lower():
            if not idx_cursor.spelling:
                logger.error("字面量的数组下标值未知")
                return problem
            try:
                idx_value = int(idx_cursor.spelling)
            except Exception:
                logger.warning("无法处理数字下标的字面量")
                return problem
            # 访问确实在界内 (0 <= idx < arr_size) 才算误报
            if 0 <= idx_value < arr_size:
                problem.set_false()
                return problem
        elif idx_cursor.kind != CursorKind.DECL_REF_EXPR:
            # 检查数组下标是否为变量
            # 从函数定义节点开始查询到这个节点为止, 并尝试发现
            pass
        elif (
            idx_cursor.get_definition()
            and idx_cursor.get_definition().kind == CursorKind.ENUM_CONSTANT_DECL
        ):
            # 是枚举引用
            idx_value = idx_cursor.get_definition().enum_value
            if 0 <= idx_value < arr_size:
                problem.set_false()
                return problem
        else:
            # 不是枚举引用的通常变量
            # 父节点约束检查暂未实现, 保守不判误报
            logger.debug("变量下标暂不检查: {}".format(idx_name))
            return problem
        return problem


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

        def _is_return_value_used(file_path: str, line_0based: int, col_0based: int) -> bool:
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
            改为从引用位置向上找到闭包函数, 再向下遍历 AST,
            通过位置比较找到最内层包含该引用的 CallExpr,
            最后检查其父节点:
            - CompoundStmt → 独立语句, 返回值未使用
            - CStyleCastExpr(→void) → 显式丢弃, 返回值未使用
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

            # 向上找到闭包函数 (semantic_parent 对声明级节点有效)
            in_func = cursor
            while in_func and in_func.kind != CursorKind.FUNCTION_DECL:
                in_func = in_func.semantic_parent
            if not in_func or in_func.kind != CursorKind.FUNCTION_DECL:
                return False

            # 1-based 位置, 用于在 AST 中定位
            ref_line = line_0based + 1
            ref_col = col_0based + 1

            def _contains(extent, line: int, col: int) -> bool:
                """检查 SourceRange 是否包含给定位置."""
                s, e = extent.start, extent.end
                if s.line > line or e.line < line:
                    return False
                if s.line == line and s.column > col:
                    return False
                if e.line == line and e.column < col:
                    return False
                return True

            def _find_innermost_call(
                node, parent
            ) -> tuple:
                """
                递归遍历 AST, 找到包含 ref_line/ref_col 的最内层 CallExpr.
                返回 (call_expr, parent_of_call_expr) 或 (None, None).
                """
                best_call = None
                best_parent = None

                for child in node.get_children():
                    if child.kind == CursorKind.CALL_EXPR:
                        extent = child.extent
                        if extent and _contains(extent, ref_line, ref_col):
                            best_call = child
                            best_parent = parent

                    inner_call, inner_parent = _find_innermost_call(child, child)
                    if inner_call:
                        best_call = inner_call
                        best_parent = inner_parent

                return best_call, best_parent

            call_expr, parent = _find_innermost_call(in_func, None)

            if not call_expr or not parent:
                return False

            # 父节点是 CompoundStmt → 独立语句, 返回值未使用
            if parent.kind == CursorKind.COMPOUND_STMT:
                return False

            # 父节点是 CStyleCastExpr 且转换类型为 void → 显式丢弃
            if parent.kind == CursorKind.CSTYLE_CAST_EXPR:
                if "void" in parent.type.spelling:
                    return False

            # 其他情况: 返回值被使用了
            return True

        func_name = problem.func_name

        # 定位所有引用该函数的代码
        all_references: list[dict] = find_references(
            code_tool.proj_dir,
            code_tool.get_command_json(problem.code_line[0].path),
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
                if _is_return_value_used(file_path, start_line, ref_loc["start"].get("character", 0)):
                    problem.clear_false()
                    logger.debug(f"发现一处使用返回值的引用(作为另一函数参数) {ref_code}")
                    problem.pro_des += f"发现一处使用返回值的引用(作为另一函数参数)"
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