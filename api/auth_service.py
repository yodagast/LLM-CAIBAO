"""用户认证服务: 密码哈希 (PBKDF2-SHA256) + 会话 cookie 令牌。

无需额外依赖 (标准库 hashlib/secrets/hmac)。
密码存储格式: pbkdf2_sha256$迭代次数$盐(hex)$哈希(hex)
会话: 登录/注册后生成随机 token, 存 sessions 表, 写入 HttpOnly cookie。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_PBKDF2_ITER = 200_000

SESSION_COOKIE = "llm_caibao_session"
SESSION_DAYS = 7


def hash_password(password: str) -> str:
    """生成密码哈希 (PBKDF2-SHA256 + 随机盐)。"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITER)
    return f"pbkdf2_sha256${_PBKDF2_ITER}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码与存储哈希是否一致 (常数时间比较)。"""
    try:
        algo, iters, salt, hexhash = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), hexhash)
    except Exception:
        return False


def new_token() -> str:
    """生成随机会话 token。"""
    return secrets.token_urlsafe(32)
