"""产业园岗位—课程—席位联动系统。

只依赖 Python 标准库。核心入口为 :class:`parkbridge.service.ParkService`。
"""

from .errors import (
    CapacityExceededError,
    CollectionInterruptedError,
    DuplicateLearnerConfirmationError,
    ParkBridgeError,
    PermanentFetchError,
    ReviewRequiredError,
    ValidationError,
)
from .db import Database
from .ontology import SkillGovernance
from .reporting import export_csv_reports
from .service import ParkService

__all__ = [
    "ParkService",
    "Database",
    "SkillGovernance",
    "export_csv_reports",
    "ParkBridgeError",
    "ValidationError",
    "CapacityExceededError",
    "DuplicateLearnerConfirmationError",
    "ReviewRequiredError",
    "CollectionInterruptedError",
    "PermanentFetchError",
]

__version__ = "1.0.0"
