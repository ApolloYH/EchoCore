"""
认证服务
处理JWT Token生成和验证
"""
import jwt
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..config import config


def _load_or_create_secret_key() -> str:
    """从配置或文件加载密钥，不存在则生成并持久化"""
    configured = config.get('auth.secret_key')
    if configured:
        return configured

    key_file = Path(__file__).parent.parent.parent / 'data' / '.jwt_secret'
    key_file.parent.mkdir(parents=True, exist_ok=True)

    if key_file.exists():
        return key_file.read_text().strip()

    new_key = secrets.token_hex(32)
    key_file.write_text(new_key)
    return new_key


class AuthService:
    """认证服务"""

    SECRET_KEY = _load_or_create_secret_key()
    ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24小时

    @classmethod
    def create_access_token(cls, user_id: str, username: str) -> str:
        """创建访问令牌"""
        expire = datetime.utcnow() + timedelta(minutes=cls.ACCESS_TOKEN_EXPIRE_MINUTES)
        payload = {
            "sub": user_id,
            "username": username,
            "exp": expire,
            "iat": datetime.utcnow()
        }
        return jwt.encode(payload, cls.SECRET_KEY, algorithm=cls.ALGORITHM)

    @classmethod
    def verify_token(cls, token: str) -> Optional[dict]:
        """验证令牌"""
        try:
            payload = jwt.decode(token, cls.SECRET_KEY, algorithms=[cls.ALGORITHM])
            return {
                "user_id": payload["sub"],
                "username": payload["username"]
            }
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            return None

    @classmethod
    def decode_token(cls, token: str) -> Optional[dict]:
        """解码令牌（不验证过期）"""
        try:
            return jwt.decode(token, cls.SECRET_KEY, algorithms=[cls.ALGORITHM], options={"verify_exp": False})
        except jwt.InvalidTokenError:
            return None
