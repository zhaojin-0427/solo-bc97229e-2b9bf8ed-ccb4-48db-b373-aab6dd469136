"""业务错误类型，由 FastAPI 异常处理器统一转换为 JSON 响应。"""


class AppError(Exception):
    """可预期的业务错误（请求内容不满足业务规则）。

    status_code 默认 400；revision 不存在等场景可传 404。
    """

    def __init__(self, code: str, message: str, status_code: int = 400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
