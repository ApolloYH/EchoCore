"""
ASR WebSocket客户端
用于与FunASR C++服务器通信
"""
import asyncio
import json
from typing import Any, Callable, Dict, Optional

import websockets

from ..config import config


class ASRClient:
    """ASR WebSocket客户端 - 增强版（带重连机制和线程安全）"""

    def __init__(self, host: str = None, port: int = None):
        self.host = host or config.asr.get('host', 'localhost')
        self.port = port or config.asr.get('port', 10095)
        self.uri = f"ws://{self.host}:{self.port}"
        self.websocket = None
        self._connected = False
        self._connect_lock = asyncio.Lock()
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 5
        self._reconnect_delay = 1.0
        self._reconnect_timer = None

    async def connect(self) -> bool:
        """建立WebSocket连接（带超时和锁）"""
        async with self._connect_lock:
            if self._connected and self.websocket:
                return True

            try:
                self.websocket = await asyncio.wait_for(
                    websockets.connect(
                        self.uri,
                        subprotocols=["binary"],
                        ping_interval=30,  # 30秒心跳
                        ping_timeout=10
                    ),
                    timeout=10.0  # 10秒连接超时
                )
                self._connected = True
                self._reconnect_attempts = 0
                return True
            except asyncio.TimeoutError:
                print("ASR connection timeout")
                return False
            except Exception as e:
                print(f"ASR connection error: {e}")
                self._connected = False
                return False

    async def reconnect(self) -> bool:
        """尝试重连（带指数退避）"""
        if self._reconnect_attempts >= self._max_reconnect_attempts:
            print("Max reconnection attempts reached")
            return False

        self._reconnect_attempts += 1
        delay = self._reconnect_delay * (2 ** (self._reconnect_attempts - 1))

        print(f"Scheduling reconnect attempt {self._reconnect_attempts} in {delay}s")

        await asyncio.sleep(delay)
        await self.disconnect()
        return await self.connect()

    async def _reconnect_and_resend(self, mode: str, wav_name: str, hotwords: Dict = None):
        """重连并重新发送配置"""
        success = await self.reconnect()
        if success:
            # 重新发送配置
            await self.send_config(mode, wav_name, hotwords)
            print("Reconnected successfully")

    def disconnect(self) -> None:
        """断开连接"""
        if self._reconnect_timer:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None

        if self.websocket:
            try:
                asyncio.create_task(self.websocket.close())
            except Exception:
                pass
            self.websocket = None
        self._connected = False

    async def send_config(self, mode: str = "2pass", wav_name: str = "meeting",
                          hotwords: Optional[Dict[str, int]] = None,
                          is_speaking: bool = True) -> None:
        """发送配置消息"""
        if not self.websocket:
            raise Exception("Not connected to ASR server")

        request = {
            "chunk_size": [5, 10, 5],
            "wav_name": wav_name,
            "is_speaking": is_speaking,
            "chunk_interval": 10,
            "mode": mode,
            "itn": True
        }

        if hotwords:
            request["hotwords"] = hotwords

        await self.websocket.send(json.dumps(request))

    async def send_audio(self, audio_data: bytes) -> None:
        """发送音频数据"""
        if not self.websocket:
            raise Exception("Not connected to ASR server")

        await self.websocket.send(audio_data)

    async def send_stop_speaking(self) -> None:
        """发送停止说话信号"""
        if not self.websocket:
            raise Exception("Not connected to ASR server")

        request = {
            "is_speaking": False
        }
        await self.websocket.send(json.dumps(request))

    async def receive(self) -> Dict[str, Any]:
        """接收识别结果"""
        if not self.websocket:
            raise Exception("Not connected to ASR server")

        message = await self.websocket.recv()

        if isinstance(message, str):
            return json.loads(message)
        else:
            # 二进制消息
            return {"type": "binary", "data": message}

    async def receive_stream(self, callback: Callable[[Dict[str, Any]], None],
                              on_close: Callable = None) -> None:
        """接收流式识别结果"""
        try:
            async for message in self.websocket:
                if isinstance(message, str):
                    result = json.loads(message)
                    callback(result)
                else:
                    callback({"type": "binary", "data": message})
        except websockets.ConnectionClosed:
            print("ASR connection closed")
            self._connected = False
            # 触发重连
            if on_close:
                on_close()
        except Exception as e:
            print(f"Error in receive stream: {e}")
            self._connected = False

    @property
    def is_connected(self) -> bool:
        """检查连接状态"""
        return self._connected


class ASRSession:
    """ASR会话管理"""

    def __init__(self, meeting_id: str = None):
        self.meeting_id = meeting_id or "default"
        self.client = ASRClient()
        self.mode = "2pass"
        self.hotwords = None
        self._text_buffer = ""
        self._is_configured = False

    async def start(self, mode: str = "2pass",
                    hotwords: Optional[Dict[str, int]] = None) -> bool:
        """开始ASR会话"""
        self.mode = mode
        self.hotwords = hotwords

        connected = await self.client.connect()
        if connected:
            await self.client.send_config(
                mode=mode,
                wav_name=self.meeting_id,
                hotwords=hotwords,
                is_speaking=True
            )
            self._is_configured = True

        return connected

    async def process_audio(self, audio_data: bytes) -> Optional[Dict[str, Any]]:
        """处理音频数据"""
        if not self._is_configured:
            # 重新配置
            await self.client.send_config(
                mode=self.mode,
                wav_name=self.meeting_id,
                hotwords=self.hotwords,
                is_speaking=True
            )
            self._is_configured = True

        await self.client.send_audio(audio_data)

        try:
            # 非阻塞尝试接收结果
            result = await asyncio.wait_for(
                self.client.receive(),
                timeout=0.1
            )
            return result
        except asyncio.TimeoutError:
            return None

    async def stop(self) -> Optional[Dict[str, Any]]:
        """停止ASR会话"""
        await self.client.send_stop_speaking()

        try:
            # 等待最终结果
            result = await asyncio.wait_for(
                self.client.receive(),
                timeout=5.0
            )
            return result
        except asyncio.TimeoutError:
            return None
        finally:
            self.client.disconnect()
            self._is_configured = False

    def get_transcript(self) -> str:
        """获取当前转写文本"""
        return self._text_buffer
