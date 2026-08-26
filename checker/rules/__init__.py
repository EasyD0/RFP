"""
checker.rules - 各检查规则的实现.

导入本包即触发所有规则注册到 CheckerDict.
"""

from .rule_69d import Checker_69D
from .rule_57s import Checker_57S
from .rule_1x import Checker_1X
from .rule_47s import Checker_47S
from .rule_36s import Checker_36S
from .rule_132s import Checker_132S

__all__ = [
    "Checker_69D",
    "Checker_57S",
    "Checker_1X",
    "Checker_47S",
    "Checker_36S",
    "Checker_132S",
]
