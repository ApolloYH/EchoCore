"""
API路由模块
"""
from fastapi import APIRouter

from . import meetings, llm, offline, realtime, auth

api_router = APIRouter(prefix="/api")

# 注册子路由
api_router.include_router(meetings.router)
api_router.include_router(llm.router)
api_router.include_router(offline.router)
api_router.include_router(realtime.router)
api_router.include_router(auth.router)
