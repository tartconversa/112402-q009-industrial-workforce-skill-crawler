"""项目内统一异常类型。"""


class ParkBridgeError(Exception):
    """所有可预期业务错误的基类。"""


class ValidationError(ParkBridgeError):
    """输入或状态转换不合法。"""


class NotFoundError(ParkBridgeError):
    """引用的资源不存在。"""


class CapacityExceededError(ParkBridgeError):
    """预留席位数超过课程剩余容量。"""

    def __init__(self, course_id: str, requested: int, available: int):
        self.course_id = course_id
        self.requested = requested
        self.available = available
        super().__init__(
            f"课程 {course_id} 仅剩 {available} 个可预留席位，不能预留 {requested} 个"
        )


class DuplicateLearnerConfirmationError(ParkBridgeError):
    """同一学员已在其他机构确认名额。"""

    def __init__(self, person: str, existing_provider: str, attempted_provider: str):
        self.person = person
        self.existing_provider = existing_provider
        self.attempted_provider = attempted_provider
        super().__init__(
            f"学员 {person} 已确认机构 {existing_provider} 的名额，"
            f"不能再占用机构 {attempted_provider} 的名额"
        )


class ReviewRequiredError(ParkBridgeError):
    """推荐因岗位/课程变化处于待重新核对状态，尚不能确认。"""


class CollectionError(ParkBridgeError):
    """采集过程错误基类。"""


class TransientFetchError(CollectionError):
    """可重试的临时网络或来源中断。"""


class PermanentFetchError(CollectionError):
    """不应重试的访问规则、地址或客户端错误。"""


class CollectionInterruptedError(CollectionError):
    """重试后仍中断；本次采集不执行删除对账。"""

    def __init__(self, source_id: str, run_id: str, cause: Exception | str):
        self.source_id = source_id
        self.run_id = run_id
        self.cause = cause
        super().__init__(f"来源 {source_id} 采集中断(run={run_id}): {cause}")
