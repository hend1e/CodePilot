# backend/api/router.py
# 路由聚合（对应课件 8.6「路由聚合与 main 集成」）：main.py 只挂一个 api_router。

from fastapi import APIRouter

from backend.api.v1 import auth, health, qa, reviews, unified_chat, webhooks

api_router = APIRouter()

api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
api_router.include_router(reviews.router, prefix="/api/v1/reviews", tags=["reviews"])
api_router.include_router(webhooks.router, prefix="/api/v1/webhooks", tags=["webhooks"])
api_router.include_router(qa.router, prefix="/api/v1/rag", tags=["rag"])
api_router.include_router(unified_chat.router, prefix="/api/v1", tags=["chat"])
