"""
clang_tool.demo - 使用示例 (原 clang_tool.py 的 __main__ 演示, 移动至此).

运行方式 (在项目根目录):
    python -m clang_tool.demo
"""

from pathlib import Path

from .parse import get_cursor_at_line


if __name__ == "__main__":
    target_file = Path("./test/example.c")
    target_line = 5  # 第 5 行: f(g());
    target_col = 5  # 第 1 列 (如果不确定具体列，通常从 1 开始尝试，或者解析该行文本找到第一个非空字符)

    cursor = get_cursor_at_line(target_file, target_line, target_col)

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
