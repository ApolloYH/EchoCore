"""
用户认证模型
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
from uuid import uuid4
import hashlib

from ..config import config


class UserModel:
    """用户数据模型"""

    def __init__(self):
        self.data_dir = Path(__file__).parent.parent.parent / 'data' / 'users'
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _get_user_path(self, user_id: str) -> Path:
        """获取用户数据文件路径"""
        return self.data_dir / f"{user_id}.json"

    def _hash_password(self, password: str, salt: str = None) -> tuple:
        """加密密码"""
        if salt is None:
            salt = uuid4().hex[:16]
        hash_obj = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
        return hash_obj.hex(), salt

    def create_user(self, username: str, password: str, email: str = None) -> Dict[str, Any]:
        """创建新用户"""
        # 检查用户名是否已存在
        existing = self.get_user_by_username(username)
        if existing:
            raise ValueError("用户名已存在")

        user_id = str(uuid4())

        # 加密密码
        password_hash, salt = self._hash_password(password)

        user = {
            "id": user_id,
            "username": username,
            "password_hash": password_hash,
            "salt": salt,
            "email": email,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "last_login": None
        }

        # 保存用户
        user_path = self._get_user_path(user_id)
        with open(user_path, 'w', encoding='utf-8') as f:
            json.dump(user, f, ensure_ascii=False, indent=2)

        # 返回不带密码的用户信息
        return self._to_user_response(user)

    def verify_password(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """验证用户密码"""
        user = self.get_user_by_username(username)
        if not user:
            return None

        password_hash, _ = self._hash_password(password, user['salt'])

        if password_hash == user['password_hash']:
            # 更新最后登录时间
            user['last_login'] = datetime.now().isoformat()
            user_path = self._get_user_path(user['id'])
            with open(user_path, 'w', encoding='utf-8') as f:
                json.dump(user, f, ensure_ascii=False, indent=2)

            return self._to_user_response(user)

        return None

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """根据用户名获取用户"""
        if not self.data_dir.exists():
            return None

        for path in self.data_dir.glob('*.json'):
            with open(path, 'r', encoding='utf-8') as f:
                user = json.load(f)
                if user['username'] == username:
                    return user

        return None

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """根据ID获取用户"""
        user_path = self._get_user_path(user_id)

        if not user_path.exists():
            return None

        with open(user_path, 'r', encoding='utf-8') as f:
            return self._to_user_response(json.load(f))

    def update_user(self, user_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """更新用户信息"""
        user = self.get_user(user_id)
        if not user:
            return None

        # 读取完整用户信息
        user_path = self._get_user_path(user_id)
        with open(user_path, 'r', encoding='utf-8') as f:
            full_user = json.load(f)

        # 更新字段
        for key, value in data.items():
            if key in ('username', 'email'):
                full_user[key] = value

        full_user['updated_at'] = datetime.now().isoformat()

        # 保存
        with open(user_path, 'w', encoding='utf-8') as f:
            json.dump(full_user, f, ensure_ascii=False, indent=2)

        return self._to_user_response(full_user)

    def change_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        """修改密码"""
        user_path = self._get_user_path(user_id)

        if not user_path.exists():
            return False

        with open(user_path, 'r', encoding='utf-8') as f:
            user = json.load(f)

        # 验证旧密码
        old_hash, _ = self._hash_password(old_password, user['salt'])
        if old_hash != user['password_hash']:
            return False

        # 设置新密码
        new_hash, new_salt = self._hash_password(new_password)
        user['password_hash'] = new_hash
        user['salt'] = new_salt
        user['updated_at'] = datetime.now().isoformat()

        with open(user_path, 'w', encoding='utf-8') as f:
            json.dump(user, f, ensure_ascii=False, indent=2)

        return True

    def _to_user_response(self, user: Dict[str, Any]) -> Dict[str, Any]:
        """转换为API响应格式（不含敏感信息）"""
        return {
            "id": user["id"],
            "username": user["username"],
            "email": user.get("email"),
            "created_at": user["created_at"],
            "last_login": user.get("last_login")
        }


# 全局实例
user_model = UserModel()
