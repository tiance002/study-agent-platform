"""产品路由聚合器：会话/计划、资料/检索、学习闭环三个领域路由。

请求模型与字段上限在 `product_schemas.py`；各领域的端点实现分别在：

- `product_conversation_routes.py` —— 会话、消息与人工计划；
- `product_source_routes.py` —— 资料候选、获取、上传与项目内检索；
- `product_learning_routes.py` —— 诊断、任务流转与提交证据。

本文件只负责把三个 `APIRouter` 聚合成一个 `router`，保持
`main.py` 的 `router as product_router` 接口不变。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.product_conversation_routes import router as conversation_router
from app.api.product_learning_routes import router as learning_router
from app.api.product_source_routes import router as source_router

router = APIRouter()
router.include_router(conversation_router)
router.include_router(source_router)
router.include_router(learning_router)
