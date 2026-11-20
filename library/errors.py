"""领域错误。

- Rejection：业务规则拒绝。服务层会吞掉它并落一条 rejected 回执，
  调用方拿到的是普通回执对象，事务整体不留半截状态。
- IdempotencyConflict：同一操作 ID 携带异文重放，直接抛出，不写任何数据。
"""

from __future__ import annotations


class LibraryError(Exception):
    """所有库内异常的基类。"""


class Rejection(LibraryError):
    """一次业务拒绝，携带机器可读原因码与上下文。"""

    def __init__(self, reason: str, **detail: object) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail

    def as_dict(self) -> dict:
        return {"reason": self.reason, **self.detail}


class IdempotencyConflict(LibraryError):
    """同操作 ID 但请求指纹不一致（异文重放）。"""

    def __init__(self, op_id: str, kind: str) -> None:
        super().__init__(f"operation id {op_id!r} already used by a different {kind} request")
        self.op_id = op_id
        self.kind = kind
