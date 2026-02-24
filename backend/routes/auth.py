"""
用户认证路由
登录、注册、Token验证
"""
from fastapi import APIRouter, HTTPException, status, Header
from pydantic import BaseModel
from typing import Optional

from ..models.user import user_model
from ..services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["认证"])


class LoginRequest(BaseModel):
    """登录请求"""
    username: str
    password: str


class LoginResponse(BaseModel):
    """登录响应"""
    access_token: str
    token_type: str = "bearer"
    user: dict


class RegisterRequest(BaseModel):
    """注册请求"""
    username: str
    password: str
    email: Optional[str] = None


class RegisterResponse(BaseModel):
    """注册响应"""
    message: str
    user: dict


class TokenPayload(BaseModel):
    """Token载荷"""
    user_id: str
    username: str


@router.post("/login", response_model=LoginResponse, summary="用户登录")
async def login(request: LoginRequest):
    """
    用户登录

    - username: 用户名
    - password: 密码
    """
    username = (request.username or "").strip()
    if not username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名不能为空"
        )
    if not request.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="密码不能为空"
        )

    user = user_model.verify_password(username, request.password)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误"
        )

    # 创建token
    access_token = AuthService.create_access_token(user['id'], user['username'])

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": user
    }


@router.post("/register", response_model=LoginResponse, summary="用户注册")
async def register(request: RegisterRequest):
    """
    用户注册（注册后自动登录）

    - username: 用户名（必填）
    - password: 密码（必填）
    - email: 邮箱（可选）
    """
    try:
        username = (request.username or "").strip()
        email = (request.email or "").strip() or None

        if not username:
            raise ValueError("用户名不能为空")
        if not request.password:
            raise ValueError("密码不能为空")

        user = user_model.create_user(
            username=username,
            password=request.password,
            email=email
        )

        # 注册成功后自动创建 token（保持与登录接口返回格式一致）
        access_token = AuthService.create_access_token(user['id'], user['username'])

        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": user
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.get("/me", summary="获取当前用户信息")
async def get_current_user(Authorization: Optional[str] = Header(None)):
    """
    获取当前登录用户信息

    需要在请求头中携带: Authorization: Bearer <token>
    """
    if not Authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少认证令牌"
        )

    # 解析 Bearer token
    try:
        prefix, token = Authorization.split()
        if prefix.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效的认证格式"
            )
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证格式"
        )

    # 验证token
    token_data = AuthService.verify_token(token)
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌无效或已过期"
        )

    # 获取用户信息
    user = user_model.get_user(token_data['user_id'])
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在"
        )

    return user


@router.post("/refresh", summary="刷新访问令牌")
async def refresh_token(Authorization: Optional[str] = Header(None)):
    """
    刷新访问令牌

    需要提供有效的旧令牌
    """
    if not Authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少认证令牌"
        )

    try:
        prefix, token = Authorization.split()
        if prefix.lower() != "bearer":
            raise ValueError()
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证格式"
        )

    # 验证旧token并创建新token
    token_data = AuthService.verify_token(token)
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌无效或已过期"
        )

    new_token = AuthService.create_access_token(token_data['user_id'], token_data['username'])

    return {
        "access_token": new_token,
        "token_type": "bearer"
    }
