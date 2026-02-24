"""
实时会议总结API
支持会议进行中的实时摘要生成
"""
from datetime import datetime
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from ..services.llm_service import LLMService

router = APIRouter(prefix="/realtime", tags=["实时总结"])


class RealtimeSummaryRequest(BaseModel):
    """实时摘要请求"""
    text: str  # 新增的会议内容
    previous_summary: Optional[str] = ""  # 之前的摘要


class RealtimeSummaryResponse(BaseModel):
    """实时摘要响应"""
    topic: str  # AI判断的会议主题
    incremental: str  # 增量摘要
    turning_points: List[dict]  # AI提炼的关键转折点
    key_points: List[str]  # 关键要点
    decisions: List[str]  # 决策
    context_summary: str  # 滚动累计摘要
    generated_at: str  # 生成时间


@router.post("/summary", response_model=RealtimeSummaryResponse, summary="生成实时摘要")
async def generate_realtime_summary(request: RealtimeSummaryRequest):
    """
    会议进行中实时生成摘要

    - text: 新增的会议内容
    - previous_summary: 之前的摘要（可选，用于增量生成）
    """
    try:
        result = await LLMService.generate_realtime_summary(
            text=request.text,
            previous_summary=request.previous_summary or ""
        )

        return {
            "topic": result.get('topic', ''),
            "incremental": result['incremental'],
            "turning_points": result.get('turning_points', []),
            "key_points": result.get('key_points', []),
            "decisions": result.get('decisions', []),
            "context_summary": result.get('context_summary', ''),
            "generated_at": datetime.now().isoformat()
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成实时摘要失败: {str(e)}")


@router.get("/status", summary="检查实时总结服务状态")
async def check_status():
    """检查实时总结服务是否可用"""
    llm_available = await LLMService.is_available()

    return {
        "available": llm_available,
        "provider": "ollama"  # 可以从配置获取
    }
