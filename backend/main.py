"""
EchoCore - FastAPI主入口
"""
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import websockets

# 支持直接运行 `python main.py`（非包方式）
if __name__ == "__main__" and (__package__ is None or __package__ == ""):
    import sys

    backend_parent = Path(__file__).resolve().parent.parent
    if str(backend_parent) not in sys.path:
        sys.path.insert(0, str(backend_parent))
    __package__ = "backend"

from .config import config
from .routes import api_router
from .services import LLMService

# 配置日志
def setup_logging():
    """配置日志"""
    log_file = config.get('logging.file', 'logs/app.log')
    # 确保日志目录存在
    log_dir = os.path.dirname(os.path.abspath(log_file))
    os.makedirs(log_dir, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, config.get('logging.level', 'INFO')),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5),
            logging.StreamHandler()
        ]
    )

setup_logging()
logger = logging.getLogger(__name__)


class RateLimitMiddleware:
    """简单的速率限制中间件"""

    def __init__(self, requests_per_minute: int = 60):
        self.requests_per_minute = requests_per_minute
        self.clients: dict = {}

    async def __call__(self, request: Request, call_next):
        client_ip = request.client.host
        current_time = time.time()

        if client_ip not in self.clients:
            self.clients[client_ip] = []

        # 清理超过1分钟的请求记录
        self.clients[client_ip] = [
            t for t in self.clients[client_ip]
            if current_time - t < 60
        ]

        # 检查速率限制
        if len(self.clients[client_ip]) >= self.requests_per_minute:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please slow down."
            )

        self.clients[client_ip].append(current_time)

        return await call_next(request)


# 创建速率限制器实例 - 开发环境使用更宽松的限制
rate_limiter = RateLimitMiddleware(requests_per_minute=300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    logger.info("EchoCore 服务启动中...")

    # 检查LLM服务
    llm_available = await LLMService.is_available()
    logger.info(f"LLM服务可用: {llm_available}")

    yield

    # 关闭时
    # 清理速率限制器
    rate_limiter.clients.clear()
    logger.info("EchoCore 服务关闭")


def get_cors_origins() -> list:
    """从配置获取CORS白名单"""
    origins = config.get('cors.origins', [])
    if not origins:
        # 开发环境默认值：允许所有来源
        return ["*"]
    return origins


# 创建FastAPI应用
app = FastAPI(
    title="EchoCore API",
    description="智能会议助手，提供实时语音识别和会议总结功能",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json"
)

# CORS配置
cors_origins = get_cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=("*" not in cors_origins),  # 通配符时不能启用 credentials
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
)

# 速率限制
app.middleware("http")(rate_limiter)

# 挂载静态文件
static_dir = Path(__file__).parent.parent / 'frontend'
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# 注册API路由
app.include_router(api_router)


@app.get("/", response_class=HTMLResponse, summary="主页面")
async def root():
    """返回主页面"""
    index_path = Path(__file__).parent.parent / 'frontend' / 'index.html'
    if index_path.exists():
        with open(index_path, 'r', encoding='utf-8') as f:
            return f.read()
    else:
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <title>EchoCore</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 40px; }
                h1 { color: #333; }
                .status { padding: 20px; background: #f0f0f0; border-radius: 8px; }
            </style>
        </head>
        <body>
            <h1>EchoCore</h1>
            <div class="status">
                <p>服务运行正常！</p>
                <p>API文档: <a href="/api/docs">/api/docs</a></p>
            </div>
        </body>
        </html>
        """


def get_asr_ws_url() -> str:
    """获取后端连接ASR的WebSocket地址"""
    scheme = config.get('asr.ws_scheme', 'ws')
    host = config.asr.get('host', '127.0.0.1')
    port = config.asr.get('port', 10095)

    if host in ('0.0.0.0', '::', ''):
        host = '127.0.0.1'

    return f"{scheme}://{host}:{port}/"


@app.websocket('/ws/asr')
async def asr_ws_proxy(websocket: WebSocket):
    """将前端WebSocket代理到ASR服务，避免浏览器跨端口/协议问题"""
    raw_protocols = websocket.headers.get('sec-websocket-protocol', '')
    subprotocols = [p.strip() for p in raw_protocols.split(',') if p.strip()]
    selected_subprotocol = subprotocols[0] if subprotocols else None

    await websocket.accept(subprotocol=selected_subprotocol)

    asr_ws_url = get_asr_ws_url()
    ssl_context = None
    if asr_ws_url.startswith('wss://'):
        import ssl

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    try:
        async with websockets.connect(
            asr_ws_url,
            subprotocols=subprotocols or None,
            ssl=ssl_context,
            # ASR 是本地服务，禁用环境代理可避免 ws://127.0.0.1 被错误走 HTTP_PROXY
            proxy=None,
        ) as asr_socket:

            async def client_to_asr():
                while True:
                    message = await websocket.receive()
                    msg_type = message.get('type')

                    if msg_type == 'websocket.disconnect':
                        raise WebSocketDisconnect()

                    text_payload = message.get('text')
                    if text_payload is not None:
                        await asr_socket.send(text_payload)
                        continue

                    bytes_payload = message.get('bytes')
                    if bytes_payload is not None:
                        await asr_socket.send(bytes_payload)

            async def asr_to_client():
                while True:
                    message = await asr_socket.recv()
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            task_client_to_asr = asyncio.create_task(client_to_asr())
            task_asr_to_client = asyncio.create_task(asr_to_client())

            done, pending = await asyncio.wait(
                {task_client_to_asr, task_asr_to_client},
                return_when=asyncio.FIRST_EXCEPTION,
            )

            for task in pending:
                task.cancel()

            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    raise exc

    except WebSocketDisconnect:
        logger.info('前端ASR WebSocket连接断开')
    except Exception as exc:
        logger.error('ASR代理连接失败: %s -> %s', asr_ws_url, exc)
        try:
            await websocket.close(code=1011, reason='ASR proxy failed')
        except Exception:
            pass


@app.get("/health", summary="健康检查")
async def health_check():
    """健康检查"""
    llm_available = await LLMService.is_available()

    return {
        "status": "healthy",
        "asr_connected": True,
        "llm_available": llm_available,
        "version": "1.0.0"
    }


@app.get("/api/info", summary="服务信息")
async def api_info():
    """获取API信息"""
    return {
        "name": "EchoCore",
        "version": "1.0.0",
        "endpoints": {
            "meetings": "/api/meetings",
            "llm": "/api/llm",
            "health": "/health",
            "docs": "/api/docs"
        },
        "features": [
            "实时语音识别 (Online/Offline/2pass)",
            "会议转写",
            "LLM智能总结",
            "历史记录管理"
        ]
    }


def create_app() -> FastAPI:
    """创建应用实例"""
    return app


if __name__ == "__main__":
    import uvicorn

    host = config.web.get('host', '0.0.0.0')
    port = config.web.get('port', 8080)

    print(f"启动会议助手服务: http://{host}:{port}")
    print(f"API文档: http://{host}:{port}/api/docs")

    uvicorn.run(app, host=host, port=port, reload=config.web.get('debug', False))
