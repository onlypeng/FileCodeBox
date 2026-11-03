"""统一的 HTTP 响应包装。

所有业务接口返回 `{code, message, detail}`，由前端据此判断成败。

约定：`code` 是**业务状态码而不是 HTTP 状态码**，HTTP 层一律 200（除了少数由
FastAPI/中间件直接抛出的 4xx/5xx）。这样前端只需要处理一处错误分支，不必区分
"HTTP 错误"与"业务逻辑错误"两套取值。

`code` 默认 200 表示成功（沿用 HTTP 语义便于直觉理解），失败各处自定非零值。
`detail` 泛型化（Generic[T]）：只为让后端能对自己的载荷做类型提示，
pydantic 不会在运行时校验它，前端侧仍是自由结构。
"""
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    """业务响应壳。

    FastAPI 会用它生成 openapi schema；但 `detail` 是 Any 语义，
    同一个接口在不同分支返回不同 detail 结构时不会报错，也不计入文档。
    """

    code: int = 200
    message: str = "ok"
    detail: Optional[T] = None
