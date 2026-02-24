"""
离线长音频上传路由
支持大文件分片上传、任务状态管理、识别进度查询
"""
import asyncio
import hashlib
import json
import logging
import os
import shutil
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, BackgroundTasks, Request
from pydantic import BaseModel

from ..config import config

router = APIRouter(prefix="/offline", tags=["离线上传"])
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 配置
UPLOAD_DIR = Path(config.get('storage.upload_dir', 'data/uploads'))
TEMP_DIR = Path(config.get('storage.temp_dir', 'data/temp'))
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

# 用于按标点切句的标点集合
SENTENCE_PUNCTUATION = set("。！？!?；;\n")

# 确保目录存在
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)


class JobStatus(str, Enum):
    """任务状态"""
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    QUEUED = "queued"
    RECOGNIZING = "recognizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class OfflineJob:
    """离线识别任务"""
    def __init__(
        self,
        job_id: str,
        meeting_id: str,
        file_name: str,
        file_path: str,
        compute_device: str = "gpu"
    ):
        self.id = job_id
        self.meeting_id = meeting_id
        self.file_name = file_name
        self.file_path = file_path
        self.compute_device = (compute_device or "gpu").lower()
        self.status = JobStatus.UPLOADING
        self.status_text = "等待上传"
        self.created_at = datetime.now()
        self.updated_at = datetime.now()
        self.upload_percent = 0
        self.recognition_percent = 0
        self.error = None
        self.result = None


class OfflineManager:
    """离线任务管理器"""
    _instance = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._jobs: Dict[str, OfflineJob] = {}
            cls._instance._upload_sessions: Dict[str, dict] = {}
        return cls._instance

    async def create_upload_session(
        self,
        meeting_id: str,
        file_name: str,
        file_size: int,
        file_type: str,
        chunk_size: int = 8 * 1024 * 1024,
        mode: str = "offline",
        hotwords: dict = None,
        compute_device: str = "gpu"
    ) -> dict:
        """创建上传会话"""
        async with self._lock:
            upload_id = str(uuid.uuid4())
            compute_device = str(compute_device or "gpu").lower()
            if compute_device not in {"gpu", "cpu", "auto"}:
                compute_device = "gpu"

            # 验证文件大小
            if file_size > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件大小超过限制 ({MAX_FILE_SIZE / 1024 / 1024 / 1024:.0f}GB)"
                )

            # 验证文件类型
            valid_types = ['audio/mpeg', 'audio/wav', 'audio/mp4', 'audio/aac',
                          'audio/flac', 'audio/ogg', 'audio/x-m4a', 'audio/mp3']
            ext = Path(file_name).suffix.lower()
            valid_exts = ['.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg']

            if file_type not in valid_types and ext not in valid_exts:
                raise HTTPException(
                    status_code=400,
                    detail="不支持的文件类型，请上传有效的音频文件"
                )

            session = {
                "upload_id": upload_id,
                "meeting_id": meeting_id,
                "file_name": file_name,
                "file_size": file_size,
                "file_type": file_type,
                "chunk_size": chunk_size,
                "mode": mode,
                "hotwords": hotwords or {},
                "compute_device": compute_device,
                "uploaded_chunks": set(),
                "total_chunks": (file_size + chunk_size - 1) // chunk_size,
                "created_at": datetime.now().isoformat()
            }

            # 创建临时目录
            session_dir = TEMP_DIR / upload_id
            session_dir.mkdir(exist_ok=True)

            self._upload_sessions[upload_id] = session

            return {
                "upload_id": upload_id,
                "chunk_size": chunk_size,
                "total_chunks": session["total_chunks"],
                "uploaded_chunks": list(session["uploaded_chunks"]),
                "compute_device": compute_device,
                "max_parallel": 3
            }

    async def upload_chunk(
        self,
        upload_id: str,
        chunk_index: int,
        content: bytes
    ) -> dict:
        """上传分片"""
        if upload_id not in self._upload_sessions:
            raise HTTPException(status_code=404, detail="上传会话不存在")

        session = self._upload_sessions[upload_id]

        # 写入分片文件
        chunk_path = TEMP_DIR / upload_id / f"chunk_{chunk_index:06d}"
        with open(chunk_path, 'wb') as f:
            f.write(content)

        session["uploaded_chunks"].add(chunk_index)

        uploaded_bytes = sum(
            (chunk_index + 1) * session["chunk_size"]
            if i == len(session["uploaded_chunks"]) - 1
            else session["chunk_size"]
            for i, chunk_index in enumerate(sorted(session["uploaded_chunks"]))
        ) - session["chunk_size"] + len(content)

        # 更新上传进度
        session["upload_percent"] = min(100, (uploaded_bytes / session["file_size"]) * 100)

        return {
            "chunk_index": chunk_index,
            "uploaded_bytes": uploaded_bytes,
            "total_bytes": session["file_size"],
            "percent": session["upload_percent"]
        }

    async def complete_upload(self, upload_id: str, meeting_id: str) -> dict:
        """完成上传，合并文件"""
        if upload_id not in self._upload_sessions:
            raise HTTPException(status_code=404, detail="上传会话不存在")

        session = self._upload_sessions[upload_id]

        # 检查是否所有分片都已上传
        expected_chunks = session["total_chunks"]
        uploaded = len(session["uploaded_chunks"])

        if uploaded < expected_chunks:
            raise HTTPException(
                status_code=400,
                detail=f"分片不完整: 已上传 {uploaded}/{expected_chunks} 个分片"
            )

        # 合并文件
        job_id = str(uuid.uuid4())
        output_path = UPLOAD_DIR / f"{job_id}_{session['file_name']}"

        try:
            with open(output_path, 'wb') as outfile:
                for i in range(expected_chunks):
                    chunk_path = TEMP_DIR / upload_id / f"chunk_{i:06d}"
                    if chunk_path.exists():
                        with open(chunk_path, 'rb') as infile:
                            outfile.write(infile.read())
                        chunk_path.unlink()  # 删除分片

            # 删除临时目录
            (TEMP_DIR / upload_id).rmdir()

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"文件合并失败: {str(e)}")

        # 创建任务
        job = OfflineJob(
            job_id=job_id,
            meeting_id=meeting_id,
            file_name=session['file_name'],
            file_path=str(output_path),
            compute_device=session.get("compute_device", "gpu")
        )
        job.status = JobStatus.QUEUED
        job.status_text = "等待识别"

        self._jobs[job_id] = job
        del self._upload_sessions[upload_id]

        # 启动后台识别任务
        asyncio.create_task(self._run_recognition(job))

        return {
            "job_id": job_id,
            "status": job.status.value,
            "message": "文件上传完成，识别任务已加入队列"
        }

    async def _run_recognition(self, job: OfflineJob):
        """运行离线识别任务"""
        try:
            # 更新状态
            job.status = JobStatus.RECOGNIZING
            job.status_text = "识别中"
            job.updated_at = datetime.now()

            # 检查是否已取消
            if job.status == JobStatus.CANCELED:
                return

            # 调用实际的 ASR 识别
            success = await self._recognize_audio(job)

            if job.status == JobStatus.CANCELED:
                return

            if success:
                job.status = JobStatus.COMPLETED
                job.status_text = "识别完成"
                job.recognition_percent = 100
            else:
                job.status = JobStatus.FAILED
                job.status_text = "识别失败"
                if not job.error:
                    job.error = "识别过程出错"

        except Exception as e:
            job.status = JobStatus.FAILED
            job.status_text = "识别失败"
            job.error = str(e)

        job.updated_at = datetime.now()

    async def _recognize_audio(self, job: OfflineJob) -> bool:
        """执行音频识别"""
        try:
            # 检查是否已取消
            if job.status == JobStatus.CANCELED:
                return False

            # 动态导入，避免启动时就加载模型
            from funasr import AutoModel

            # 检查是否已取消
            if job.status == JobStatus.CANCELED:
                return False

            # 统一只走本地模型目录，禁止在线拉取模型
            model_ref = config.get(
                'offline.model_id',
                'iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch'
            )
            vad_model_ref = config.get(
                'offline.vad_model_id',
                'iic/speech_fsmn_vad_zh-cn-16k-common-pytorch'
            )
            punc_model_ref = config.get(
                'offline.punc_model_id',
                'iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch'
            )
            model_path = self._resolve_local_model_path(model_ref)
            vad_model_path = self._resolve_local_model_path(vad_model_ref)
            punc_model_path = self._resolve_local_model_path(punc_model_ref)
            if not (model_path and vad_model_path and punc_model_path):
                search_roots = ", ".join(str(p) for p in self._model_search_roots())
                missing_parts = []
                if not model_path:
                    missing_parts.append(f"model={model_ref}")
                if not vad_model_path:
                    missing_parts.append(f"vad_model={vad_model_ref}")
                if not punc_model_path:
                    missing_parts.append(f"punc_model={punc_model_ref}")
                job.error = (
                    "离线模型未找到（仅支持本地目录，不走在线下载）: "
                    + "; ".join(missing_parts)
                    + f"。搜索路径: {search_roots}"
                )
                logger.error(job.error)
                return False

            device, device_warning = self._select_device(job.compute_device)
            if device_warning:
                logger.warning("离线识别设备降级: %s", device_warning)
            logger.info(
                "离线识别模型路径: model=%s, vad=%s, punc=%s",
                model_path,
                vad_model_path,
                punc_model_path,
            )
            model_kwargs = dict(
                model=str(model_path),
                vad_model=str(vad_model_path),
                punc_model=str(punc_model_path),
                trust_remote_code=True,
                disable_update=True,
            )
            model_kwargs["device"] = device

            # 加载模型，兼容旧版 FunASR 参数差异
            asr_model, init_error = self._init_asr_model(AutoModel, model_kwargs, device)

            if asr_model is None:
                job.error = init_error or "ASR 模型加载失败"
                return False

            # 检查是否已取消
            if job.status == JobStatus.CANCELED:
                return False

            # 执行识别
            generate_kwargs = dict(
                input=job.file_path,
                batch_size_s=60,
                hotword=self._get_hotwords(job.meeting_id),
                sentence_timestamp=True,
            )
            result = asr_model.generate(**generate_kwargs)

            # 检查是否已取消
            if job.status == JobStatus.CANCELED:
                return False

            # 解析结果
            if result and len(result) > 0:
                first = result[0] if isinstance(result[0], dict) else {}
                text = str(first.get('text', '') or '').strip()
                segments = self._build_segments(first)

                # 兜底：至少保留全文
                if not segments and text:
                    segments = [{"text": text, "start_time": 0.0, "end_time": 0.0}]

                # 保存结果
                job.result = {
                    'full_text': text,
                    'segments': segments,
                    'summary': None,
                    'compute_device_preference': job.compute_device,
                    'compute_device': device,
                    'warnings': [w for w in (device_warning,) if w]
                }

                from ..services.meeting_service import meeting_service
                await meeting_service.save_offline_result(
                    job.meeting_id,
                    job.result
                )

                return True

            job.error = "识别结果为空"
            return False

        except Exception as e:
            job.error = str(e)
            logger.exception("离线识别失败: %s", e)
            return False

    def _model_search_roots(self) -> List[Path]:
        """离线模型搜索路径（源码和 Docker 共用）"""
        roots: List[Path] = []
        seen: Set[str] = set()

        def add_root(path_like: Optional[str]) -> None:
            if not path_like:
                return
            path_obj = Path(path_like).expanduser()
            if not path_obj.is_absolute():
                path_obj = (PROJECT_ROOT / path_obj).resolve()
            else:
                path_obj = path_obj.resolve()
            path_key = str(path_obj)
            if path_key not in seen:
                seen.add(path_key)
                roots.append(path_obj)

        modelscope_cache = os.environ.get("MODELSCOPE_CACHE")
        if not modelscope_cache:
            modelscope_cache = str(PROJECT_ROOT / "data" / "modelscope_cache")

        add_root(str(PROJECT_ROOT / "data" / "models"))
        add_root(str(Path(modelscope_cache) / "models"))

        extra_roots = config.get("offline.model_search_paths", [])
        if isinstance(extra_roots, str):
            extra_roots = [p.strip() for p in extra_roots.split(",") if p.strip()]
        if isinstance(extra_roots, list):
            for root in extra_roots:
                add_root(str(root))

        return roots

    def _resolve_local_model_path(self, model_ref: str) -> Optional[Path]:
        """将模型引用解析到本地目录"""
        ref = str(model_ref or "").strip()
        if not ref:
            return None

        direct_path = Path(ref).expanduser()
        if direct_path.is_absolute() and direct_path.exists():
            return direct_path.resolve()

        if not direct_path.is_absolute():
            cwd_path = (Path.cwd() / direct_path).resolve()
            if cwd_path.exists():
                return cwd_path

            project_path = (PROJECT_ROOT / direct_path).resolve()
            if project_path.exists():
                return project_path

        for root in self._model_search_roots():
            candidate = (root / ref).resolve()
            if candidate.exists():
                return candidate

        return None

    def _build_segments(self, result_item: Dict[str, Any]) -> List[dict]:
        """优先使用 sentence_info；否则用 text+timestamp 按标点切句"""
        sentence_info = result_item.get("sentence_info")
        if isinstance(sentence_info, list) and sentence_info:
            segments: List[dict] = []
            for sent in sentence_info:
                if not isinstance(sent, dict):
                    continue
                sent_text = str(sent.get("text") or sent.get("sentence") or "").strip()
                if not sent_text:
                    continue

                ts_pairs = self._normalize_timestamp_pairs(sent.get("timestamp"))
                if ts_pairs:
                    start_sec = round(ts_pairs[0][0] / 1000.0, 3)
                    end_sec = round(ts_pairs[-1][1] / 1000.0, 3)
                else:
                    # 兼容多种字段名：begin_time/end_time, start/end, start_time/end_time
                    raw_start = sent.get("begin_time") or sent.get("start") or sent.get("start_time") or 0
                    raw_end = sent.get("end_time") or sent.get("end") or sent.get("stop") or 0
                    start_sec = round(float(raw_start) / 1000.0, 3) if raw_start else 0.0
                    end_sec = round(float(raw_end) / 1000.0, 3) if raw_end else 0.0

                segments.append({"text": sent_text, "start_time": start_sec, "end_time": end_sec})
            if segments:
                return segments

        # 回退：用 text + 字级 timestamp 按标点切句
        text = str(result_item.get("text", "") or "").strip()
        return self._segments_from_text_timestamp(text, result_item.get("timestamp"))

    def _normalize_timestamp_pairs(self, raw_timestamps: Any) -> List[Tuple[float, float]]:
        """将各种格式的时间戳归一化为 [(start, end), ...] 列表"""
        pairs: List[Tuple[float, float]] = []
        if not isinstance(raw_timestamps, list):
            return pairs
        for item in raw_timestamps:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                start, end = float(item[0]), float(item[1])
            except (TypeError, ValueError):
                continue
            pairs.append((start, end))
        return pairs

    def _segments_from_text_timestamp(self, text: str, raw_timestamps: Any) -> List[dict]:
        """根据 FunASR 的字级 timestamp（毫秒）按标点切句。
        注意：标点由 punc 模型后处理插入，timestamp 不含标点位，标点不消耗索引。
        """
        ts_pairs = self._normalize_timestamp_pairs(raw_timestamps)
        if not text or not ts_pairs:
            return []

        # 标点和空白字符不消耗 timestamp 索引
        punctuation_set = SENTENCE_PUNCTUATION | set("，,、：:""''\"'()（）【】[]《》<>—…·")

        segments: List[dict] = []
        current_chars: List[str] = []
        current_start_ms: Optional[float] = None
        current_end_ms: Optional[float] = None
        ts_idx = 0

        def flush_segment():
            nonlocal current_chars, current_start_ms, current_end_ms
            sentence = "".join(current_chars).strip()
            if sentence:
                start_sec = round(current_start_ms / 1000.0, 3) if current_start_ms is not None else 0.0
                end_sec = round(current_end_ms / 1000.0, 3) if current_end_ms is not None else 0.0
                segments.append({"text": sentence, "start_time": start_sec, "end_time": end_sec})
            current_chars = []
            current_start_ms = None
            current_end_ms = None

        for ch in text:
            current_chars.append(ch)
            # 只有非标点、非空白的实际语音字符才消耗一个 timestamp
            is_speech_char = not ch.isspace() and ch not in punctuation_set
            if is_speech_char and ts_idx < len(ts_pairs):
                start_ms, end_ms = ts_pairs[ts_idx]
                ts_idx += 1
                if current_start_ms is None:
                    current_start_ms = start_ms
                current_end_ms = end_ms
            if ch in SENTENCE_PUNCTUATION:
                flush_segment()

        flush_segment()
        return segments

    def _select_device(self, preferred_device: str = "gpu") -> Tuple[str, Optional[str]]:
        """选择离线识别设备"""
        preferred = str(preferred_device or "gpu").lower()
        if preferred == "cpu":
            logger.info("离线识别按配置使用 CPU")
            return "cpu", None
        try:
            import torch
            if torch.cuda.is_available():
                device_name = torch.cuda.get_device_name(0)
                logger.info("离线识别使用 GPU: %s", device_name)
                return "cuda:0", None
            logger.info("未检测到可用 GPU，离线识别使用 CPU")
            warning = "已选择 GPU，但当前环境未检测到可用 CUDA，已自动切换到 CPU"
            return "cpu", warning if preferred == "gpu" else None
        except ImportError:
            logger.warning("未安装 PyTorch，离线识别使用 CPU")
            warning = "未检测到可用 PyTorch CUDA 环境，已自动切换到 CPU"
            return "cpu", warning if preferred == "gpu" else None

    def _init_asr_model(self, auto_model_cls, model_kwargs: dict, device: str):
        """加载 ASR 模型，自动兼容旧版 FunASR 参数差异。
        返回 (model, error_message)。加载失败时 model 为 None。"""
        kwargs = dict(model_kwargs)
        try:
            return auto_model_cls(**kwargs), None
        except TypeError as exc:
            exc_msg = str(exc)
            changed = False
            if "device" in exc_msg and "device" in kwargs:
                kwargs.pop("device", None)
                kwargs["ngpu"] = 1 if device.startswith("cuda") else 0
                logger.warning("当前 FunASR 版本不支持 device 参数，回退为 ngpu=%s", kwargs["ngpu"])
                changed = True
            if "disable_update" in exc_msg and "disable_update" in kwargs:
                kwargs.pop("disable_update", None)
                logger.warning("当前 FunASR 版本不支持 disable_update 参数，已移除")
                changed = True
            if changed:
                try:
                    return auto_model_cls(**kwargs), None
                except Exception as retry_exc:
                    logger.exception("ASR 模型加载重试失败: %s", retry_exc)
                    return None, str(retry_exc)
            logger.exception("ASR 模型加载失败: %s", exc)
            return None, exc_msg
        except Exception as exc:
            logger.exception("ASR 模型加载失败: %s", exc)
            return None, str(exc)

    def _get_hotwords(self, meeting_id: str) -> str:
        """获取热词"""
        hotword_path = UPLOAD_DIR / 'hotwords.txt'
        if hotword_path.exists():
            return hotword_path.read_text().strip()
        return ""

    async def get_job(self, job_id: str) -> Optional[dict]:
        """获取任务状态"""
        if job_id not in self._jobs:
            raise HTTPException(status_code=404, detail="任务不存在")

        job = self._jobs[job_id]

        return {
            "job_id": job.id,
            "meeting_id": job.meeting_id,
            "compute_device_preference": job.compute_device,
            "status": job.status.value,
            "status_text": job.status_text,
            "percent": job.recognition_percent,
            "upload": {
                "percent": getattr(job, 'upload_percent', 100),
                "file_name": job.file_name
            },
            "recognition": {
                "percent": job.recognition_percent
            },
            "result": job.result,
            "error": job.error,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat()
        }

    async def cancel_job(self, job_id: str) -> dict:
        """取消任务"""
        if job_id not in self._jobs:
            raise HTTPException(status_code=404, detail="任务不存在")

        job = self._jobs[job_id]

        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED):
            raise HTTPException(status_code=400, detail="任务无法取消")

        job.status = JobStatus.CANCELED
        job.status_text = "已取消"
        job.updated_at = datetime.now()

        # 删除临时文件
        if Path(job.file_path).exists():
            Path(job.file_path).unlink()

        return {"status": "canceled", "message": "任务已取消"}


# 全局管理器
offline_manager = OfflineManager()


# ==================== API 端点 ====================

@router.post("/uploads/init", summary="初始化上传会话")
async def init_upload(request: Request):
    """
    初始化分片上传会话
    接受 JSON 格式请求体
    """
    try:
        body = await request.json()
        import json

        result = await offline_manager.create_upload_session(
            meeting_id=body.get('meeting_id'),
            file_name=body.get('file_name'),
            file_size=body.get('file_size'),
            file_type=body.get('file_type'),
            chunk_size=body.get('chunk_size', 8 * 1024 * 1024),
            mode=body.get('mode', 'offline'),
            hotwords=body.get('hotwords', {}),
            compute_device=body.get('compute_device', 'gpu')
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/uploads/{upload_id}/chunks/{chunk_index}", summary="上传分片")
async def upload_chunk(
    upload_id: str,
    chunk_index: int,
    request: Request
):
    """上传单个分片 - 接受原始二进制数据"""
    try:
        content = await request.body()
        result = await offline_manager.upload_chunk(upload_id, chunk_index, content)
        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/uploads/{upload_id}/complete", summary="完成上传")
async def complete_upload(upload_id: str, request: Request):
    """
    完成上传，合并文件并启动识别
    接受 JSON 格式请求体
    """
    try:
        body = await request.json()

        result = await offline_manager.complete_upload(
            upload_id=upload_id,
            meeting_id=body.get('meeting_id')
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/jobs/{job_id}", summary="获取任务状态")
async def get_job_status(job_id: str):
    """获取离线识别任务状态"""
    try:
        result = await offline_manager.get_job(job_id)
        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/jobs/{job_id}/cancel", summary="取消任务")
async def cancel_job(job_id: str):
    """取消离线识别任务"""
    try:
        result = await offline_manager.cancel_job(job_id)
        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/jobs/{job_id}/result", summary="获取识别结果")
async def get_job_result(job_id: str):
    """获取离线识别完整结果"""
    try:
        job = await offline_manager.get_job(job_id)

        if job["status"] != "completed":
            raise HTTPException(
                status_code=400,
                detail=f"任务未完成，当前状态: {job['status_text']}"
            )

        return job["result"]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
