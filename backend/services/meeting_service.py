"""
会议管理服务
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from ..config import config


class MeetingService:
    """会议服务类"""

    def __init__(self):
        self.data_dir = Path(__file__).parent.parent.parent / 'data' / 'meetings'
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _get_meeting_path(self, meeting_id: str) -> Path:
        """获取会议数据文件路径"""
        return self.data_dir / f"{meeting_id}.json"

    async def create_meeting(self, name: str, mode: str = "2pass",
                             user_id: str = None,
                             hotwords: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
        """创建新会议"""
        meeting_id = str(uuid4())

        meeting = {
            "id": meeting_id,
            "user_id": user_id,  # 所属用户ID
            "name": name,
            "mode": mode,
            "status": "active",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "duration": 0,
            "hotwords": hotwords or {},
            "transcript": "",
            "transcript_segments": [],
            "summary": None,
            "audio_chunks": []
        }

        # 保存到文件
        meeting_path = self._get_meeting_path(meeting_id)
        with open(meeting_path, 'w', encoding='utf-8') as f:
            json.dump(meeting, f, ensure_ascii=False, indent=2)

        return meeting

    async def get_meeting(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        """获取会议详情"""
        meeting_path = self._get_meeting_path(meeting_id)

        if not meeting_path.exists():
            return None

        with open(meeting_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    async def list_meetings(self, limit: int = 20, offset: int = 0, user_id: str = None) -> List[Dict[str, Any]]:
        """获取会议列表"""
        meetings = []

        if self.data_dir.exists():
            for path in sorted(self.data_dir.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
                with open(path, 'r', encoding='utf-8') as f:
                    meeting = json.load(f)

                    # 用户数据隔离：过滤非该用户的会议
                    if user_id and meeting.get('user_id') != user_id:
                        continue

                    # 简化为列表项
                    meetings.append({
                        "id": meeting["id"],
                        "name": meeting["name"],
                        "mode": meeting["mode"],
                        "status": meeting["status"],
                        "created_at": meeting["created_at"],
                        "duration": meeting.get("duration", 0)
                    })

        return meetings[offset:offset + limit]

    async def update_transcript(self, meeting_id: str, text: str,
                                 segment: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """更新会议 transcript"""
        meeting = await self.get_meeting(meeting_id)
        if not meeting:
            return None

        # 追加到现有 transcript
        if meeting["transcript"]:
            meeting["transcript"] += "\n" + text
        else:
            meeting["transcript"] = text

        # 保存片段
        if segment:
            meeting["transcript_segments"].append(segment)

        meeting["updated_at"] = datetime.now().isoformat()

        # 重新计算duration
        if meeting["transcript_segments"]:
            last_segment = meeting["transcript_segments"][-1]
            meeting["duration"] = int(last_segment.get("end_time", 0))

        # 保存
        meeting_path = self._get_meeting_path(meeting_id)
        with open(meeting_path, 'w', encoding='utf-8') as f:
            json.dump(meeting, f, ensure_ascii=False, indent=2)

        return meeting

    async def end_meeting(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        """结束会议"""
        meeting = await self.get_meeting(meeting_id)
        if not meeting:
            return None

        meeting["status"] = "completed"
        meeting["updated_at"] = datetime.now().isoformat()

        # 计算最终duration
        if meeting["transcript_segments"]:
            last_segment = meeting["transcript_segments"][-1]
            meeting["duration"] = int(last_segment.get("end_time", 0))

        # 保存
        meeting_path = self._get_meeting_path(meeting_id)
        with open(meeting_path, 'w', encoding='utf-8') as f:
            json.dump(meeting, f, ensure_ascii=False, indent=2)

        return meeting

    async def delete_meeting(self, meeting_id: str) -> bool:
        """删除会议"""
        meeting_path = self._get_meeting_path(meeting_id)

        if meeting_path.exists():
            meeting_path.unlink()
            return True

        return False

    async def save_summary(self, meeting_id: str, summary: str,
                           key_points: List[str] = None,
                           todos: List[Dict[str, str]] = None,
                           decisions: List[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
        """保存会议总结"""
        meeting = await self.get_meeting(meeting_id)
        if not meeting:
            return None

        meeting["summary"] = {
            "content": summary,
            "key_points": key_points or [],
            "todos": todos or [],
            "decisions": decisions or [],
            "generated_at": datetime.now().isoformat()
        }
        meeting["updated_at"] = datetime.now().isoformat()

        # 保存
        meeting_path = self._get_meeting_path(meeting_id)
        with open(meeting_path, 'w', encoding='utf-8') as f:
            json.dump(meeting, f, ensure_ascii=False, indent=2)

        return meeting

    async def search_transcripts(self, query: str, limit: int = 10, user_id: str = None) -> List[Dict[str, Any]]:
        """搜索会议记录"""
        results = []

        if self.data_dir.exists():
            for path in self.data_dir.glob('*.json'):
                with open(path, 'r', encoding='utf-8') as f:
                    meeting = json.load(f)

                    # 用户数据隔离
                    if user_id and meeting.get('user_id') != user_id:
                        continue

                    transcript = meeting.get("transcript", "")

                    if query.lower() in transcript.lower():
                        results.append({
                            "id": meeting["id"],
                            "name": meeting["name"],
                            "created_at": meeting["created_at"],
                            "matched_text": transcript[:200] + "..." if len(transcript) > 200 else transcript
                        })

        return results[:limit]

    async def save_offline_result(self, meeting_id: str, result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """保存离线识别结果"""
        meeting = await self.get_meeting(meeting_id)
        if not meeting:
            return None

        # 解析结果
        full_text = result.get('full_text', '')
        segments = result.get('segments', [])

        # 更新会议数据
        meeting["transcript"] = full_text
        meeting["transcript_segments"] = segments
        meeting["status"] = "completed"
        meeting["updated_at"] = datetime.now().isoformat()

        # 计算duration
        if segments:
            last_segment = segments[-1]
            meeting["duration"] = int(last_segment.get("end_time", 0))

        # 保存
        meeting_path = self._get_meeting_path(meeting_id)
        with open(meeting_path, 'w', encoding='utf-8') as f:
            json.dump(meeting, f, ensure_ascii=False, indent=2)

        return meeting


# 全局服务实例
meeting_service = MeetingService()
