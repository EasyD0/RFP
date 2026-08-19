"""
checker - 静态检查框架.

对外统一导出公共 API, 保持与原 checker.py 单文件模块兼容
(test.py 等调用方无需修改 import 语句).

子模块职责:
- base:    检查器基类 / 装饰器 / 注册表
- context: 编译上下文
- rules:   各规则实现 (导入即注册)
- runner:  串行 / 多进程调度
"""

from .base import (
    Checker,
    CheckerDict,
    Checker_isUsed,
    common_method,
    register_checker,
    tag_padding,
    un_used,
)
from .context import CodeContext
from . import rules as _rules  # noqa: F401  (导入即触发规则注册)

# 将通用检查附加到每条规则 (原 checker.py 模块底部的注册逻辑)
for _k, _v in CheckerDict.items():
    CheckerDict[_k].add(Checker_isUsed)

from .runner import total_check, total_check_parallel
from .rules.rule_69d import Checker_69D
from .rules.rule_57s import Checker_57S
from .rules.rule_1x import Checker_1X
from .rules.rule_47s import Checker_47S
from .rules.rule_36s import Checker_36S

__all__ = [
    "Checker",
    "CheckerDict",
    "Checker_isUsed",
    "CodeContext",
    "register_checker",
    "tag_padding",
    "common_method",
    "un_used",
    "total_check",
    "total_check_parallel",
    "Checker_69D",
    "Checker_57S",
    "Checker_1X",
    "Checker_47S",
    "Checker_36S",
]
