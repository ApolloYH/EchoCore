"""
会议管理API路由
"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from ..models import MeetingCreate, MeetingResponse, MeetingListItem
from ..services import meeting_service
from ..services.auth_service import AuthService

router = APIRouter(prefix="/meetings", tags=["meetings"])

# HTTP Bearer 认证
security = HTTPBearer()


async def get_current_user(Authorization: Optional[str] = Header(None)) -> dict:
    """获取当前用户（可选认证）"""
    if not Authorization:
        return None

    try:
        prefix, token = Authorization.split()
        if prefix.lower() != "bearer":
            return None
    except (ValueError, AttributeError):
        return None

    token_data = AuthService.verify_token(token)
    if not token_data:
        return None

    return {
        "user_id": token_data["user_id"],
        "username": token_data["username"]
    }


async def require_auth(Authorization: Optional[str] = Header(None)) -> dict:
    """获取当前用户（需要有效的 token，否则抛出 401）"""
    if not Authorization:
        raise HTTPException(status_code=401, detail="请先登录后再使用")

    try:
        prefix, token = Authorization.split()
        if prefix.lower() != "bearer":
            raise HTTPException(status_code=401, detail="无效的认证格式")
    except (ValueError, AttributeError):
        raise HTTPException(status_code=401, detail="无效的认证格式")

    token_data = AuthService.verify_token(token)
    if not token_data:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")

    return {
        "user_id": token_data["user_id"],
        "username": token_data["username"]
    }


@router.post("", response_model=MeetingResponse, summary="创建新会议")
async def create_meeting(request: MeetingCreate, current_user: dict = Depends(require_auth)):
    """创建新会议（需要登录）"""
    meeting = await meeting_service.create_meeting(
        name=request.name,
        mode=request.mode,
        user_id=current_user["user_id"],
        hotwords=request.hotwords
    )
    return meeting


@router.get("", response_model=List[MeetingListItem], summary="获取会议列表")
async def list_meetings(limit: int = 20, offset: int = 0, current_user: dict = Depends(get_current_user)):
    """获取会议列表"""
    user_id = current_user["user_id"] if current_user else None

    meetings = await meeting_service.list_meetings(
        limit=limit,
        offset=offset,
        user_id=user_id
    )
    return meetings


@router.get("/{meeting_id}", response_model=MeetingResponse, summary="获取会议详情")
async def get_meeting(meeting_id: str):
    """获取会议详情"""
    meeting = await meeting_service.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.post("/{meeting_id}/transcript", summary="更新会议转写")
async def update_transcript(meeting_id: str, data: dict):
    """更新会议转写文本"""
    meeting = await meeting_service.update_transcript(
        meeting_id=meeting_id,
        text=data.get("text", ""),
        segment=data.get("segment")
    )
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return {"success": True}


@router.post("/{meeting_id}/end", response_model=MeetingResponse, summary="结束会议")
async def end_meeting(meeting_id: str):
    """结束会议"""
    meeting = await meeting_service.end_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.delete("/{meeting_id}", summary="删除会议")
async def delete_meeting(meeting_id: str, current_user: dict = Depends(require_auth)):
    """删除会议（仅允许删除自己的会议）"""
    meeting = await meeting_service.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    owner_id = meeting.get("user_id")
    if owner_id and owner_id != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="无权删除该会议")

    success = await meeting_service.delete_meeting(meeting_id)
    if not success:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return {"success": True}


@router.get("/{meeting_id}/transcript", summary="获取会议转写")
async def get_transcript(meeting_id: str):
    """获取会议完整转写"""
    meeting = await meeting_service.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return {
        "id": meeting["id"],
        "name": meeting["name"],
        "transcript": meeting.get("transcript", ""),
        "segments": meeting.get("transcript_segments", []),
        "duration": meeting.get("duration", 0)
    }


@router.get("/{meeting_id}/summary", summary="获取会议总结")
async def get_summary(meeting_id: str):
    """获取会议总结"""
    meeting = await meeting_service.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting.get("summary", {})


@router.get("/search", summary="搜索会议")
async def search_meetings(query: str, limit: int = 10, current_user: dict = Depends(get_current_user)):
    """搜索会议记录"""
    user_id = current_user["user_id"] if current_user else None

    results = await meeting_service.search_transcripts(query, limit=limit, user_id=user_id)
    return results
