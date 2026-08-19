"""
clang_tool.parse - 源码解析与游标定位.

提供 "文件 + 行列 -> Cursor" 的完整链路:
- parse_tu:        解析翻译单元, 可复用同一个 TU 取多个游标, 避免重复解析
- cursor_at:       在已解析的 TU 中按行列取游标
- get_cursor_at_line / get_cursor_in_pos: 便捷入口
- get_cursor_text: 从游标覆盖范围读取对应源码文本
"""

from pathlib import Path

from clang.cindex import (
    Cursor,
    File,
    Index,
    SourceLocation,
    TranslationUnit,
    TranslationUnitLoadError,
)
from MyPyLib.LogSet import logSetup
from data_structure import CodePos

from .config import CLANG_INC

logger = logSetup(__name__)


def parse_tu(
    file_path: Path, args: list | None = None, options: int = 0
) -> TranslationUnit | None:
    """
    解析单个源文件为翻译单元, 供后续多次取游标复用; 解析失败返回 None.

    :param file_path: 源文件路径
    :param args: 额外编译参数 (如宏定义 / 头文件路径)
    :param options: libclang 解析选项, 如 TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
    :return: TranslationUnit 或 None
    """
    try:
        index = Index.create()
        tu = index.parse(
            file_path.as_posix(),
            args=[*(args or []), CLANG_INC, "-x", "c"],
            options=options,
        )
    except TranslationUnitLoadError as e:
        logger.error(f"解析失败：{e}")
        return None
    return tu


def cursor_at(
    tu: TranslationUnit, file_path: Path, line_num: int, col_num: int
) -> Cursor | None:
    """
    在已解析的翻译单元中, 按行列获取游标.

    :param tu: 由 parse_tu 得到的翻译单元
    :param file_path: 源文件路径
    :param line_num: 行号 (从 1 开始)
    :param col_num: 列号 (从 1 开始)
    :return: Cursor, 位置无效时返回 None
    """
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
    # 若行列是空白区域，可能会返回父节点或空节点
    logger.error("文件和行列位置不对")
    return None


def get_cursor_at_line(
    file_path: Path, line_num: int, col_num: int = 1, args: list | None = None
) -> Cursor | None:
    """
    根据文件路径、行号和列号获取 Clang Cursor 对象。

    :param file_path: 源代码文件的绝对路径
    :param line_num: 行号 (从 1 开始)
    :param col_num: 列号 (从 1 开始，如果不确定，通常设为 1 或该行第一个非空白字符位置)
    :param args: 额外编译参数
    :return: clang.cindex.Cursor 对象，如果位置无效则返回 None
    """
    tu = parse_tu(file_path, args)
    if tu is None:
        return None
    return cursor_at(tu, file_path, line_num, col_num)


def find_column(code_pos: CodePos, token: str = "", proj_dir=None) -> int:
    """
    找到代码文本 token 所在的列.

    :param code_pos: 代码文本的文件路径和行号
    :param token: 目标代码文本, 若为空则用 CodePos.code 替代
    :return: 1-based 列号
    :raises ValueError: 无法在行文本中找到 token 时
    """
    with open(
        (proj_dir or Path("/")) / code_pos.path,
        "r",
        encoding="utf-8",
        errors="replace",
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
    raise ValueError("无法找到列位置")


def get_cursor_in_pos(
    code_pos: CodePos, proj_dir: Path, args: list | None = None, token: str = ""
) -> Cursor | None:
    source_file: Path = code_pos.path
    line = code_pos.line
    column = find_column(code_pos, token, proj_dir)
    return get_cursor_at_line(proj_dir / source_file, line, column, args)


def get_cursor_text(cur: Cursor) -> str:
    """读取游标覆盖的完整源码文本 (可能跨多行), 供文本匹配使用"""
    src_file = cur.extent.start.file
    if not src_file:
        return ""
    with open(Path(str(src_file)), "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    start_line = cur.extent.start.line
    end_line = cur.extent.end.line
    return "".join(lines[start_line - 1 : end_line])
