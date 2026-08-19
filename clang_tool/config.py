"""
clang_tool.config - Clang 解析相关配置.

集中管理 libclang 动态库路径与 C 编译参数, 支持通过环境变量覆盖:
- LIBCLANG_DIR: libclang 动态库所在目录
- CLANG_INC: clang 内置头文件 include 参数
"""

import os

from clang.cindex import Config

# libclang 动态库所在目录 (可通过环境变量 LIBCLANG_DIR 覆盖)
LIBCLANG_DIR = os.environ.get("LIBCLANG_DIR", r"D:\Conda_Env\codeA\Library\bin")

# 编译 C 源码时附加的 clang 内置头文件搜索路径 (可通过环境变量 CLANG_INC 覆盖)
CLANG_INC = os.environ.get(
    "CLANG_INC", r"-ID:\Program Files\LLVM\lib\clang\18\include"
)

Config.set_library_path(LIBCLANG_DIR)
# Config.set_library_file(libclang_path)  # 当未打包时, 该路径实际上为 "./libclang.dll"
