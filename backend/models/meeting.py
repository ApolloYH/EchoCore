"""
数据模型定义
"""
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class MeetingCreate(BaseModel):
    """创建会议请求"""
    name: str = Field(..., description="会议名称")
    mode: str = Field(default="2pass", description="识别模式: online/offline/2pass")
    hotwords: Optional[Dict[str, int]] = Field(default=None, description="热词字典")


class MeetingResponse(BaseModel):
    """会议响应"""
    id: str
    name: str
    mode: str
    status: str
    created_at: datetime
    updated_at: datetime
    duration: Optional[int] = None  # 会议时长（秒）
    transcript: Optional[str] = None  # 完整 transcript
    summary: Optional[str] = None  # LLM总结

    class Config:
        from_attributes = True


class TranscriptSegment(BaseModel):
    """语音识别片段"""
    text: str
    start_time: float
    end_time: float
    is_final: bool


class AudioChunk(BaseModel):
    """音频块（WebSocket传输用）"""
    mode: str
    wav_name: str
    is_speaking: bool
    chunk_size: List[int]
    chunk_interval: int
    audio_data: Optional[bytes] = None  # 二进制音频数据


class ASRResult(BaseModel):
    """ASR识别结果"""
    mode: str
    text: str
    wav_name: str
    is_final: bool
    timestamp: Optional[List[List[int]]] = None


class SummarizeRequest(BaseModel):
    """总结请求"""
    text: str = Field(..., description="需要总结的文本")
    options: Optional[Dict[str, Any]] = Field(default=None, description="总结选项")


class SummarizeResponse(BaseModel):
    """总结响应"""
    summary: str
    key_points: List[str]
    todos: List[Dict[str, str]]
    decisions: List[Dict[str, str]]


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str
    asr_connected: bool
    llm_available: bool


class MeetingListItem(BaseModel):
    """会议列表项"""
    id: str
    name: str
    mode: str
    status: str
    created_at: datetime
    duration: Optional[int] = None
