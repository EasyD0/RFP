"""
checker.rules.rule_404s - 规则 404S: 字符串数组的赋值必须在所分配的空间之内.

problem的各个字段raw_text形如:
代码行形的 raw_text 形如:
    ../path.c:38
    Modu_Pos_t_S SRoof_Thread_Area[96]=  {

规则名称的 raw_text 形如:
    字符串数组的赋值必须在所分配的空间之内 : SRoof_Thread_Area[*]; given=672, expected=96
"""

from pathlib import Path
from MyPyLib.LogSet import logSetup
from clang.cindex import Cursor, TypeKind

from clang_tool import parse_tu, get_cursor_in_pos, get_cursor_text
from data_structure import Problem
from ..context import CodeContext
from ..base import (
    Checker,
    common_method,
    register_checker,
    tag_padding,
    
)


from pathlib import Path
import re
from pathlib import Path


def get_the_array_define_code(file: Path, begin_line: int) -> str:
    """
    打开文件 file 第 begin_line 行,获取该行代码,并向下扩展,
    直到找到完整的数组定义代码(处理跨行块注释、字符串/字符字面量、
    嵌套花括号,准确判断语句结束位置)。
    :params begin_line: 数组定义的起始行号,行号从 1 开始计数
    :return: 从起始行到语句结束(含分号)的完整源码文本
    """
    lines = file.read_text(encoding='utf-8').splitlines(keepends=True)

    if not (1 <= begin_line <= len(lines)):
        raise ValueError(f"begin_line={begin_line} 超出文件行数范围(共 {len(lines)} 行)")

    STATE_NORMAL = 0
    STATE_STRING = 1
    STATE_CHAR = 2
    STATE_LINE_COMMENT = 3
    STATE_BLOCK_COMMENT = 4

    state = STATE_NORMAL
    brace_depth = 0
    collected = []

    for line_idx in range(begin_line - 1, len(lines)):
        line = lines[line_idx]
        collected.append(line)

        i = 0
        n = len(line)
        finished = False

        while i < n:
            c = line[i]
            nxt = line[i + 1] if i + 1 < n else ''

            if state == STATE_NORMAL:
                if c == '/' and nxt == '/':
                    state = STATE_LINE_COMMENT
                    i += 2
                    continue
                elif c == '/' and nxt == '*':
                    state = STATE_BLOCK_COMMENT
                    i += 2
                    continue
                elif c == '"':
                    state = STATE_STRING
                    i += 1
                    continue
                elif c == "'":
                    state = STATE_CHAR
                    i += 1
                    continue
                elif c == '{':
                    brace_depth += 1
                    i += 1
                    continue
                elif c == '}':
                    brace_depth -= 1
                    i += 1
                    continue
                elif c == ';' and brace_depth == 0:
                    finished = True
                    i += 1
                    break
                else:
                    i += 1
                    continue

            elif state == STATE_STRING:
                if c == '\\' and i + 1 < n:
                    i += 2
                    continue
                elif c == '"':
                    state = STATE_NORMAL
                i += 1
                continue

            elif state == STATE_CHAR:
                if c == '\\' and i + 1 < n:
                    i += 2
                    continue
                elif c == "'":
                    state = STATE_NORMAL
                i += 1
                continue

            elif state == STATE_LINE_COMMENT:
                i += 1
                continue

            elif state == STATE_BLOCK_COMMENT:
                if c == '*' and nxt == '/':
                    state = STATE_NORMAL
                    i += 2
                    continue
                else:
                    i += 1
                    continue

        # 单行注释在换行处自动结束
        if state == STATE_LINE_COMMENT:
            state = STATE_NORMAL

        if finished:
            break

    return ''.join(collected)

def rm_c_comment(code: str) -> str:
    """
    去除 C 代码中的注释,同时保证结果的行数与原文件一致。
    正确处理字符串字面量 "..." 和字符字面量 '...' 内部的
    // 和 /* 等符号,不会误删。
    """
    result = []
    i = 0
    n = len(code)
 
    STATE_NORMAL = 0
    STATE_STRING = 1
    STATE_CHAR = 2
    STATE_LINE_COMMENT = 3
    STATE_BLOCK_COMMENT = 4
 
    state = STATE_NORMAL
 
    while i < n:
        c = code[i]
        nxt = code[i + 1] if i + 1 < n else ''
 
        if state == STATE_NORMAL:
            if c == '/' and nxt == '/':
                state = STATE_LINE_COMMENT
                i += 2
                continue
            elif c == '/' and nxt == '*':
                state = STATE_BLOCK_COMMENT
                i += 2
                continue
            elif c == '"':
                state = STATE_STRING
                result.append(c)
                i += 1
                continue
            elif c == "'":
                state = STATE_CHAR
                result.append(c)
                i += 1
                continue
            else:
                result.append(c)
                i += 1
                continue
 
        elif state == STATE_STRING:
            result.append(c)
            if c == '\\' and i + 1 < n:
                # 转义字符,连同下一个字符一起原样保留
                result.append(nxt)
                i += 2
                continue
            elif c == '"':
                state = STATE_NORMAL
            i += 1
            continue
 
        elif state == STATE_CHAR:
            result.append(c)
            if c == '\\' and i + 1 < n:
                result.append(nxt)
                i += 2
                continue
            elif c == "'":
                state = STATE_NORMAL
            i += 1
            continue
 
        elif state == STATE_LINE_COMMENT:
            if c == '\n':
                # 单行注释结束于换行符,保留该换行符以维持行数
                result.append(c)
                state = STATE_NORMAL
            # 注释内容本身全部丢弃
            i += 1
            continue
 
        elif state == STATE_BLOCK_COMMENT:
            if c == '\n':
                # 块注释内部的换行符必须保留,否则行数会变
                result.append(c)
                i += 1
                continue
            elif c == '*' and nxt == '/':
                state = STATE_NORMAL
                i += 2
                continue
            else:
                # 块注释内的其他字符直接丢弃
                i += 1
                continue
 
    return ''.join(result)

def get_the_sharpInc(s: str) -> list[Path]:
    """
    获取代码片段里 #include 的内容
    :param s: 输入的代码片段
    :return:
    """
    # 允许 # 前有空白(预处理指令允许缩进),#和include之间也允许空白
    # 匹配 "xxx.h" 或 <xxx.h> 两种形式
    pattern = re.compile(
        r'^[ \t]*#[ \t]*include[ \t]+(?:"([^"]+)"|<([^>]+)>)',
        re.MULTILINE
    )

    result = []
    for m in pattern.finditer(s):
        name = m.group(1) or m.group(2)
        result.append(Path(name))
    return result

def get_the_array_dimention(arrary_decl:str):
    """
    从数组声明的文本中获取数组的维度
    例如从"typeA arr[3][4][5] = {" 中获取维度 3
    """
    # 不考虑类型别名的情况
    res = 0;
    for c in arrary_decl:
        if c == "[":
            res += 1
        if c in {"=", ";", ",", "{"}:
            break
    return res

def get_the_array_size(array_decl: str) -> list[int]:
    """
    从数组声明的文本中获取数组的每个维度的大小
    例如从"typeA arr[3][4][5] = {" 中获取 [3,4,5]
    """
    sizes = []
    pattern = re.compile(r'\[(\d+)\]')
    for match in pattern.finditer(array_decl):
        sizes.append(int(match.group(1)))
    return sizes

def _split_top_level(text: str) -> list[str]:
    """按花括号深度为 0 处的逗号切分,同时跳过字符串/字符字面量内部的逗号和花括号"""
    STATE_NORMAL, STATE_STRING, STATE_CHAR = 0, 1, 2
    state = STATE_NORMAL
    depth = 0
    current = []
    elements = []
    i, n = 0, len(text)

    while i < n:
        c = text[i]
        if state == STATE_NORMAL:
            if c == '"':
                state = STATE_STRING
                current.append(c)
                i += 1
                continue
            elif c == "'":
                state = STATE_CHAR
                current.append(c)
                i += 1
                continue
            elif c == '{':
                depth += 1
                current.append(c)
                i += 1
                continue
            elif c == '}':
                depth -= 1
                current.append(c)
                i += 1
                continue
            elif c == ',' and depth == 0:
                elements.append(''.join(current))
                current = []
                i += 1
                continue
            else:
                current.append(c)
                i += 1
                continue
        elif state == STATE_STRING:
            current.append(c)
            if c == '\\' and i + 1 < n:
                current.append(text[i + 1])
                i += 2
                continue
            elif c == '"':
                state = STATE_NORMAL
            i += 1
            continue
        elif state == STATE_CHAR:
            current.append(c)
            if c == '\\' and i + 1 < n:
                current.append(text[i + 1])
                i += 2
                continue
            elif c == "'":
                state = STATE_NORMAL
            i += 1
            continue

    elements.append(''.join(current))
    return [e.strip() for e in elements if e.strip() != '']


def _strip_outer_braces(s: str) -> str | None:
    """如果 s 整体被一对花括号完整包裹,返回内部内容;否则返回 None"""
    s = s.strip()
    if not (s.startswith('{') and s.endswith('}')):
        return None
    depth = 0
    for idx, c in enumerate(s):
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and idx != len(s) - 1:
                return None
    return s[1:-1]


def get_the_array_initlist_dimention(arrary_initlist: str, dimention: int) -> list[int]:
    """
    根据数组初始化列表的内容, 计算每个维度最大的大小
    :param arrary_initlist: 初始化器的文本
    :param dimention: 数组的维度
    """
    if dimention <= 0:
        return []

    elements = _split_top_level(arrary_initlist)
    count = len(elements)
    result = [count]

    if dimention > 1:
        sub_max = [0] * (dimention - 1)
        for el in elements:
            inner = _strip_outer_braces(el)
            if inner is None:
                continue
            sub_sizes = get_the_array_initlist_dimention(inner, dimention - 1)
            for i, v in enumerate(sub_sizes):
                sub_max[i] = max(sub_max[i], v)
        result.extend(sub_max)

    return result

def find_file_in_include_options(file: str, compile_args: list[str], proj_dir: Path) -> Path | None:
    """
    file: 是 include 后的文件, 可能是相对路径, 可能是文件名
    compile_args: 是编译参数列表, 含有 -I 路径
    proj_dir: 绝对路径 是工程根目录, 用于相对路径的搜索
    return: 返回找到的文件的绝对路径, 如果找不到返回 None
    """
    file_path = Path(file)

    # 1. 如果本身就是绝对路径,直接判断存在与否
    if file_path.is_absolute():
        return file_path if file_path.is_file() else None

    # 2. 提取 compile_args 里的 -I 路径,保持原始顺序(编译器按顺序搜索,先匹配先用)
    include_dirs: list[Path] = []
    i = 0
    n = len(compile_args)
    while i < n:
        arg = compile_args[i]
        if arg == '-I':
            # 分开写: -I path
            if i + 1 < n:
                include_dirs.append(Path(compile_args[i + 1]))
                i += 2
                continue
        elif arg.startswith('-I') and len(arg) > 2:
            # 连写: -Ipath
            include_dirs.append(Path(arg[2:]))
        i += 1

    # 把相对的 -I 路径相对 proj_dir 展开成绝对路径
    resolved_dirs = []
    for d in include_dirs:
        resolved_dirs.append(d if d.is_absolute() else (proj_dir / d))

    # 3. 先试相对工程根目录直接拼接(常见的写法:include "a/b.h" 就是相对 proj_dir)
    candidate = proj_dir / file_path
    if candidate.is_file():
        return candidate.resolve()

    # 4. 依次尝试每个 -I 目录
    for d in resolved_dirs:
        candidate = d / file_path
        if candidate.is_file():
            return candidate.resolve()

    return None


logger = logSetup(__name__)


@register_checker("404S", "字符串数组的赋值必须在所分配的空间之内")
class Checker_404S(Checker):
    @common_method
    @tag_padding("<初始化列表数量合规-纯文本算法>")
    @staticmethod
    def func0(problem: Problem, code_tool: CodeContext) -> Problem:
        """
        通过查找包含的头文件里数组元素的个数, 与数组定义的长度进行比较, 考虑多维数组的情况
        """
        code_line_num = problem.code_line[0].line
        code_file = problem.file_path1(code_tool.proj_dir)

        arrary_define_code = get_the_array_define_code(code_file, code_line_num)
        arrary_define_code = rm_c_comment(arrary_define_code)
        arrary_dimention = get_the_array_dimention(arrary_define_code.split("\n")[0])
        arrary_sizes = get_the_array_size(arrary_define_code.split("\n")[0])

        if len(arrary_sizes) != arrary_dimention:
            logger.warning(f"数组定义的维度与大小不匹配, 可能存在宏定义等, 从文本上无法判别, 维度={arrary_dimention}, 大小={arrary_sizes}")
            return problem

        include_files = get_the_sharpInc(arrary_define_code)

        code_file_dir = code_file.parent
        compile_args:list[str] = code_tool.get_args(code_file) # 包含编译参数 -I 的参数列表 

        all_inc_file_text_list = []
        for inc_file in include_files:
            inc_file_path = (code_file_dir / inc_file).resolve()
            if not inc_file_path.exists():
                inc_file_path = find_file_in_include_options(str(inc_file), compile_args, code_tool.proj_dir)
            if not inc_file_path or not inc_file_path.exists():
                continue

            inc_file_text = inc_file_path.read_text(encoding='utf-8')
            inc_file_text = rm_c_comment(inc_file_text)
            all_inc_file_text_list.append(inc_file_text)

        all_inc_file_text = " ".join(all_inc_file_text_list)
        init_sizes = get_the_array_initlist_dimention(all_inc_file_text, arrary_dimention)
        for i in range(len(init_sizes)):
            if init_sizes[i] > arrary_sizes[i]:
                break
        else:
            problem.set_false() # 所有维度都不超过声明大小
        return problem




    @tag_padding("<初始化列表数量合规-libclang识别>")
    @staticmethod
    def func1(problem: Problem, code_tool: CodeContext) -> Problem:
        # 从规则名称获取数组名称
        arr_name = ""

        args = code_tool.get_args(problem.code_line[0].path)
        arr_cursor = get_cursor_in_pos(
            problem.code_line[0], code_tool.proj_dir, args, token=arr_name
        )
        if not arr_cursor:
            logger.warning("Cursor解析错误, 直接退出")
            return problem

        _flag = False
        if arr_cursor.type.kind != TypeKind.CONSTANTARRAY:
            logger.warning("数组类型不是TypeKind.CONSTANTARRAY, 不计算元素个数")
        else:
            arr_size = arr_cursor.type.get_array_size()
            init_size = len(list(arr_cursor.get_children()))
            if init_size > arr_size:
                # TODO 这里怎么处理比较好, 要不要看下面的诊断信息
                _flag = True

        if not _flag:
            return problem

        # 获取代码范围的文本
        arr_code_text = get_cursor_text(arr_cursor)
        inc_list = get_the_sharpInc(arr_code_text)

        tu = arr_cursor.translation_unit
        diagnose = list(tu.diagnostics)
        for diag in diagnose:
            if "excess elements in array initializer" not in diag.spelling:
                # 跳过无关问题
                continue

            bug_file = Path(str(diag.location.file)).resolve()

            # 这里要模糊匹配
            if bug_file in inc_list:
                # 出错的文件是在数组定义的#include 里
                problem.set_false()
                return problem

        return problem


