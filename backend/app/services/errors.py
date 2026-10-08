"""服务层的公开业务错误；Web 边界负责映射为响应。"""


class ServiceError(Exception):
    """携带稳定公开错误码与文字，不包含供应商原始异常。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
