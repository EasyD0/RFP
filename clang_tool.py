from pathlib import Path
import re

from clang.cindex import (
    Index,
    TranslationUnitLoadError,
    TranslationUnit,
    Config,
    Cursor,
    CursorKind,
    File,
    SourceLocation,
)
import os
from MyPyLib.LogSet import logSetup

from data_structure import CodePos

logger = logSetup(__name__)

CLANG_INC = r"-ID:\Program Files\LLVM\lib\clang\18\include"
Config.set_library_path(r"D:\Conda_Env\codeA\Library\bin")
# Config.set_library_file(libclang_path)  # 当未打包时, 该路径实际上为 "./libclang.dll"


def parse_int_literal(text: str) -> int | None:
    """
    解析 C 整型字面量文本为数值.

    支持十进制、0x/0b/0o 前缀, 以及 u/U/l/L/i64 等后缀; 解析失败返回 None.
    注意 ``010`` 按 C 语义解析为八进制 8.
    """
    t = text.strip()
    # C 整型字面量必须以数字开头 (0x/0b/0o/十进制/八进制), 否则无法解析
    if not t or not t[0].isdigit():
        return None
    # 去掉整型后缀: 2u / 2UL / 2ll / 2i64 等
    t = re.sub(r"(?:[uUlL]+|i\d+)$", "", t)
    try:
        return int(t, 0)
    except ValueError:
        pass
    # C 八进制: 0 开头且只含 0-7 (Python 的 int("010", 0) 会拒绝前导零)
    if re.fullmatch(r"0[0-7]*", t):
        return int(t, 8)
    # 0 开头但含 8/9: C 中是非法的八进制字面量, 按无法解析处理
    if re.fullmatch(r"0[0-9]+", t):
        return None
    try:
        return int(t, 10)
    except ValueError:
        return None


def literal_value_from_cursor(cursor: Cursor) -> int | None:
    s, e = cursor.extent.start, cursor.extent.end
    if s.line != e.line or e.column <= s.column:
        return None
    try:
        with open(cursor.location.file, "r", encoding="utf-8", errors="ignore") as f:
            line_text = f.readlines()[s.line - 1]
    except Exception:
        return None
    # libclang 列按 UTF-8 字节计, 按字节区间切取再解码
    line_bytes = line_text.encode("utf-8", errors="ignore")
    return parse_int_literal(
        line_bytes[s.column - 1 : e.column - 1].decode("utf-8", errors="ignore")
    )


def get_macro_int_value(
    file_path, line_num: int, col_num: int, args: list | None = None
) -> int | None:
    """
    解析宏名位置处的简单对象宏的整型字面量值.

    仅支持 ``#define NAME <整型字面量>`` (可带括号, 如 ``#define N (3)``);
    复杂替换列表 (如 ``#define F 2+2``) 或解析失败返回 None.
    """
    try:
        index = Index.create()
        tu = index.parse(
            str(file_path),
            args=[*(args or []), CLANG_INC, "-x", "c"],
            options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
        )
    except TranslationUnitLoadError as e:
        logger.error(f"解析失败：{e}")
        return None

    source_file = tu.get_file(file_path)
    if not source_file:
        return None

    location = tu.get_location(source_file.name, (line_num, col_num))
    cursor = Cursor.from_location(tu, location)
    if not cursor or cursor.kind != CursorKind.MACRO_INSTANTIATION:
        return None

    definition = cursor.get_definition()
    if not definition:
        return None
    try:
        tokens = list(definition.get_tokens())
    except Exception:
        return None
    if len(tokens) < 2:
        return None

    # 定义 tokens 形如 [宏名, 值...], 第一个是宏名
    value_tokens = [t.spelling for t in tokens[1:]]
    # 去掉外层括号: #define N (3)  →  tokens 为 ['N', '(', '3', ')']
    while len(value_tokens) >= 2 and value_tokens[0] == "(" and value_tokens[-1] == ")":
        value_tokens = value_tokens[1:-1]
    if len(value_tokens) != 1:
        return None
    return parse_int_literal(value_tokens[0])


def find_colum(code_pos: CodePos, token: str = "", proj_dir=None) -> int:
    """
    找到代码文本token所在的列

    :param code_pos: 代码文本的文件路径和行号
    :param token: 目标代码文本, 若为空 则用 CodePos.code 替代
    :return:
    """
    with open(
        (proj_dir or Path("/")) / code_pos.path, "r", encoding="utf-8", errors="replace"
    ) as f:
        lines = f.readlines()
    this_line_code = lines[code_pos.line - 1]
    if token or code_pos.token:
        res = this_line_code.find(token or code_pos.token)
    else:
        logger.error("没有token信息")
        res = -1
    if res >= 0:
        return res + 1
    else:
        raise ValueError("无法找到列位置")


def get_cursor_at_line(
    file_path: Path, line_num: int, col_num: int = 1, args: list | None = None
) -> Cursor | None:
    """
    根据文件路径、行号和列号获取 Clang Cursor 对象。

    :param file_path: 源代码文件的绝对路径
    :param line_num: 行号 (从 1 开始)
    :param col_num: 列号 (从 1 开始，如果不确定，通常设为 1 或该行第一个非空白字符位置)
    :return: clang.cindex.Cursor 对象，如果位置无效则返回 None
    """

    try:
        index = Index.create()
        tu = index.parse(
            file_path.as_posix(), args=[*(args or []), CLANG_INC, "-x", "c"]
        )
    except TranslationUnitLoadError as e:
        logger.error(f"解析失败：{e}")
        return None

    source_file: File = tu.get_file(file_path.as_posix())
    if not source_file:
        logger.error(f"未在翻译单元中找到文件：{file_path}")
        return None

    location: SourceLocation = tu.get_location(source_file.name, (line_num, col_num))
    cursor = Cursor.from_location(tu, location)

    if (
        cursor
        and cursor.location.file
        and cursor.location.file.name == source_file.name
    ):
        return cursor
    else:
        # 若行列是空白区域，可能会返回父节点或空节点
        logger.error("文件和行列位置不对")
        return None


def get_cursor_in_pos(
    code_pos: CodePos, proj_dir: Path, args: list | None = None, token: str = ""
) -> Cursor | None:
    source_file: Path = code_pos.path
    line = code_pos.line
    colum = find_colum(code_pos, token, proj_dir)
    return get_cursor_at_line(proj_dir / source_file, line, colum, args)


def _loc_key(loc) -> tuple[int, int]:
    # 用于比较源码位置
    return (loc.line, loc.column)


def cursor_contains(node: Cursor, target: Cursor) -> bool:
    """判断 node 的源码范围是否包含 target 的位置"""
    if str(node.extent.start.file) != str(target.extent.start.file):
        return False
    return (
        _loc_key(node.extent.start)
        <= _loc_key(target.extent.start)
        <= _loc_key(node.extent.end)
    )


def get_cursor_in_func(node: Cursor) -> Cursor | None:
    """从整个翻译单元中找到包含 node 的函数定义节点"""
    tu = node.translation_unit
    if not tu:
        return None

    def _find(root: Cursor) -> Cursor | None:
        if root.kind == CursorKind.FUNCTION_DECL and cursor_contains(root, node):
            return root
        for child in root.get_children():
            res = _find(child)
            if res:
                return res
        return None

    return _find(tu.cursor)


def get_innermost_block(func_root: Cursor, node: Cursor) -> Cursor | None:
    """
    找到包含 node 的最内层复合语句块, 即 node 所在块作用域
    :param func_root:
    :param node:
    :return:
    """
    best_node: Cursor | None = None
    best_closeness: tuple[int, int] | None = None

    def _walk(cur: Cursor):
        nonlocal best_node, best_closeness
        if cur.kind == CursorKind.COMPOUND_STMT and cursor_contains(cur, node):
            # 接近度指标
            closeness = (
                cur.extent.end.line - cur.extent.start.line,
                cur.extent.end.column - cur.extent.start.column,
            )
            if best_node is None or closeness < best_closeness:
                best_node, best_closeness = cur, closeness
        for child in cur.get_children():
            _walk(child)

    _walk(func_root)
    return best_node


def get_same_level_nodes(block: Cursor, node: Cursor) -> list[Cursor]:
    """
    收集与引用语句同一层级、且起始位置不晚于引用位置的兄弟语句
    TODO 这里可能有问题, 如果if for 等语句后面没有{
    """
    pos = _loc_key(node.extent.start)
    result = []
    for child in block.get_children():
        if str(child.extent.start.file) != str(node.extent.start.file):
            continue
        if _loc_key(child.extent.start) <= pos:
            result.append(child)
    return result


def get_cursor_text(cur: Cursor) -> str:
    # 读取游标覆盖的完整源码文本 (可能跨多行), 供文本匹配使用
    src_file = cur.extent.start.file
    if not src_file:
        return ""
    with open(Path(str(src_file)), "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    start_line = cur.extent.start.line
    end_line = cur.extent.end.line
    return "".join(lines[start_line - 1 : end_line])


# --- 使用示例 ---
if __name__ == "__main__":
    target_file = "./test_proj/example.c"
    target_line = 7  # 第 5 行
    target_col = 9  # 第 1 列 (如果不确定具体列，通常从 1 开始尝试，或者解析该行文本找到第一个非空字符)

    # 为了演示，先创建一个简单的测试文件

    cursor = get_cursor_at_line(Path(target_file), target_line, target_col)

    if cursor:
        print(f"成功获取 Cursor:")
        print(f"  拼写 (Spelling): {cursor.spelling}")
        print(f"  种类 (Kind): {cursor.kind}")
        print(f"  类型 (Type): {cursor.type.spelling}")
        print(
            f"  位置: {cursor.location.file}:{cursor.location.line}:{cursor.location.column}"
        )

        # 遍历子节点看看
        print("  子节点:")
        for child in cursor.get_children():
            print(f"    - {child.kind}: {child.spelling}")
    else:
        print("未在指定位置找到有效的 Cursor (可能是空白行或解析错误)。")

    print(1)