from pathlib import Path

from clang.cindex import (
    Index,
    TranslationUnitLoadError,
    Config,
    Cursor,
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
        tu = index.parse(str(file_path), args=[*(args or []), CLANG_INC, "-x", "c"])
    except TranslationUnitLoadError as e:
        logger.error(f"解析失败：{e}")
        return None

    source_file: File = tu.get_file(file_path)
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
    source_file = code_pos.path
    line = code_pos.line
    colum = find_colum(code_pos, token, proj_dir)
    return get_cursor_at_line(proj_dir / source_file, line, colum, args)


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