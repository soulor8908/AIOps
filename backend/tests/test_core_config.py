"""core/config.py — 生产环境 JWT 弱密钥 fail-fast 校验测试（security.spec.md§2.3/§9）。

覆盖：
- 开发/测试环境允许默认占位密钥 'change-me'
- 生产环境拒绝默认占位密钥
- 生产环境拒绝 < 32 字节密钥
- 生产环境接受 ≥ 32 字节强密钥
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _make_settings(**overrides: str) -> Settings:
    """构造 Settings，默认 environment=development，允许覆盖。"""
    return Settings(**overrides)


def test_dev_environment_allows_default_secret() -> None:
    """开发环境允许默认占位密钥（不抛错）。"""
    s = _make_settings(environment="development")
    assert s.effective_secret_key == "change-me"


def test_test_environment_allows_default_secret() -> None:
    """test 环境也允许默认占位密钥。"""
    s = _make_settings(environment="test")
    assert s.effective_secret_key == "change-me"


def test_production_rejects_default_secret() -> None:
    """生产环境使用默认占位密钥应启动失败。"""
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(environment="production")
    assert "JWT_SECRET" in str(exc_info.value)


def test_staging_rejects_default_secret() -> None:
    """staging 环境同样 fail-fast。"""
    with pytest.raises(ValidationError):
        _make_settings(environment="staging")


def test_production_rejects_short_secret() -> None:
    """生产环境密钥 < 32 字节应失败。"""
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(
            environment="production",
            jwt_secret="tooshort",  # 8 字节
        )
    assert "32" in str(exc_info.value)


def test_production_accepts_strong_secret() -> None:
    """生产环境提供 ≥ 32 字节强密钥应通过。"""
    strong = "x" * 48  # 48 字节
    s = _make_settings(environment="production", jwt_secret=strong)
    assert s.effective_secret_key == strong


def test_production_secret_key_alias_fallback_rejected() -> None:
    """仅设置 SECRET_KEY 别名但仍是占位值时，生产环境也应 fail-fast。"""
    with pytest.raises(ValidationError):
        _make_settings(
            environment="production",
            jwt_secret="change-me",
            secret_key="change-me",
        )


def test_environment_case_insensitive() -> None:
    """environment 大小写不敏感（Production/PRODUCTION 均触发）。"""
    with pytest.raises(ValidationError):
        _make_settings(environment="Production")
    with pytest.raises(ValidationError):
        _make_settings(environment="PROD")


# ===================== CORS 校验（security.spec.md§4）=====================


def test_cors_wildcard_rejected() -> None:
    """CORS_ORIGINS 含 '*' 应启动失败（allow_credentials=True 不可与通配 Origin 共存）。"""
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(cors_origins=["*"])
    assert "CORS_ORIGINS" in str(exc_info.value)


def test_cors_wildcard_in_list_rejected() -> None:
    """CORS_ORIGINS 列表中混入 '*' 也应拒绝。"""
    with pytest.raises(ValidationError):
        _make_settings(cors_origins=["https://ok.example.com", "*"])


def test_cors_explicit_origins_accepted() -> None:
    """显式 Origin 列表应通过校验。"""
    s = _make_settings(cors_origins=["https://console.example.com"])
    assert s.cors_origins == ["https://console.example.com"]


def test_cors_production_rejects_wildcard() -> None:
    """生产环境即使提供强密钥，CORS 含 '*' 仍应失败。"""
    strong = "x" * 48
    with pytest.raises(ValidationError) as exc_info:
        _make_settings(
            environment="production",
            jwt_secret=strong,
            cors_origins=["*"],
        )
    assert "CORS_ORIGINS" in str(exc_info.value)
