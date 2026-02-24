"""
LLM API路由
"""
from fastapi import APIRouter, HTTPException

from ..models import SummarizeRequest, SummarizeResponse
from ..services import LLMService

router = APIRouter(prefix="/llm", tags=["llm"])


@router.post("/summarize", response_model=SummarizeResponse, summary="文本总结")
async def summarize(request: SummarizeRequest):
    """对文本进行总结"""
    try:
        result = await LLMService.summarize(
            text=request.text,
            options=request.options
        )
        return SummarizeResponse(
            summary=result.get('summary', ''),
            key_points=result.get('key_points', []),
            todos=result.get('todos', []),
            decisions=result.get('decisions', [])
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status", summary="LLM服务状态")
async def llm_status():
    """检查LLM服务是否可用"""
    available = await LLMService.is_available()
    return {"available": available}


@router.post("/extract-todos", summary="提取待办事项")
async def extract_todos(data: dict):
    """从文本中提取待办事项"""
    text = data.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    try:
        result = await LLMService.summarize(
            text=text,
            options={
                "extract_todos": True,
                "summary_length": "brief"
            }
        )
        return {"todos": result.get('todos', [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/extract-decisions", summary="提取决策事项")
async def extract_decisions(data: dict):
    """从文本中提取决策事项"""
    text = data.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    try:
        result = await LLMService.summarize(
            text=text,
            options={
                "extract_decisions": True,
                "summary_length": "brief"
            }
        )
        return {"decisions": result.get('decisions', [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
