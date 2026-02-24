"""
配置加载模块
"""
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


class Config:
    """配置管理类"""

    _instance: Optional['Config'] = None
    _config: Dict[str, Any] = {}

    def __new__(cls) -> 'Config':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self) -> None:
        """加载配置文件"""
        config_path = Path(__file__).parent.parent / 'config' / 'settings.yaml'

        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f) or {}
        else:
            self._config = {}

    def reload(self) -> None:
        """重新加载配置"""
        self._load_config()

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值，支持点号分隔的路径"""
        keys = key.split('.')
        value = self._config

        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default

            if value is None:
                return default

        # 支持环境变量覆盖
        env_key = key.upper().replace('.', '_')
        env_value = os.environ.get(env_key)
        if env_value is not None:
            return env_value

        return value

    @property
    def asr(self) -> Dict[str, Any]:
        """获取ASR配置"""
        return self._config.get('asr', {})

    @property
    def web(self) -> Dict[str, Any]:
        """获取Web配置"""
        return self._config.get('web', {})

    @property
    def llm(self) -> Dict[str, Any]:
        """获取LLM配置"""
        return self._config.get('llm', {})

    @property
    def database(self) -> Dict[str, Any]:
        """获取数据库配置"""
        return self._config.get('database', {})

    @property
    def frontend(self) -> Dict[str, Any]:
        """获取前端配置"""
        return self._config.get('frontend', {})


# 全局配置实例
config = Config()
