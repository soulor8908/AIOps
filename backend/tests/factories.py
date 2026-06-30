"""测试数据工厂 — 遵循 testing.spec.md§8 factory pattern。

所有测试数据通过工厂构造，禁止散落字面量。每个工厂提供 ``build()``
返回 Pydantic schema 与 ``create_via_api()`` 通过 TestClient 创建并返回 JSON。
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

# ===================== Auth =====================

class UserFactory:
    """用户测试数据工厂。"""

    _counter = 0

    @classmethod
    def _next(cls) -> int:
        cls._counter += 1
        return cls._counter

    @classmethod
    def build_create(
        cls,
        *,
        email: str | None = None,
        username: str | None = None,
        password: str = "TestPass123!",
        full_name: str | None = "Test User",
    ) -> dict[str, Any]:
        n = cls._next()
        return {
            "email": email or f"user{n}@test.example",
            "username": username or f"testuser{n}",
            "password": password,
            "full_name": full_name,
        }

    @classmethod
    def create_via_api(
        cls,
        client: TestClient,
        **overrides: Any,
    ) -> dict[str, Any]:
        payload = cls.build_create(**overrides)
        resp = client.post("/api/v1/auth/register", json=payload)
        assert resp.status_code == 201, resp.text
        return resp.json()

    @classmethod
    def login_via_api(
        cls,
        client: TestClient,
        *,
        email: str,
        password: str,
    ) -> dict[str, Any]:
        """通过 /auth/token 登录，返回 token 包。"""
        resp = client.post(
            "/api/v1/auth/token",
            data={"username": email, "password": password},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    @classmethod
    def create_and_login(
        cls,
        client: TestClient,
        **overrides: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """注册 + 登录，返回 (user_out, token_pack)。"""
        payload = cls.build_create(**overrides)
        user = cls.create_via_api(client, **payload)
        tokens = cls.login_via_api(
            client, email=payload["email"], password=payload["password"]
        )
        return user, tokens


# ===================== Prompts =====================

class PromptFactory:
    """Prompt 测试数据工厂。"""

    _counter = 0

    @classmethod
    def build_create(
        cls,
        *,
        name: str | None = None,
        content: str = "Hello {{name}}!",
    ) -> dict[str, Any]:
        cls._counter += 1
        return {
            "name": name or f"test-prompt-{cls._counter}",
            "content": content,
        }

    @classmethod
    def create_via_api(
        cls,
        client: TestClient,
        **overrides: Any,
    ) -> dict[str, Any]:
        payload = cls.build_create(**overrides)
        resp = client.post("/api/v1/prompts", json=payload)
        assert resp.status_code == 201, resp.text
        return resp.json()


# ===================== Agents =====================

class AgentFactory:
    """Agent 测试数据工厂。"""

    _counter = 0

    @classmethod
    def build_create(
        cls,
        *,
        name: str | None = None,
        system_prompt: str = "you are a helpful agent",
    ) -> dict[str, Any]:
        cls._counter += 1
        return {
            "name": name or f"test-agent-{cls._counter}",
            "system_prompt": system_prompt,
            "model_alias": "default",
            "tools": [],
            "max_turns": 3,
            "temperature": 0.7,
        }

    @classmethod
    def create_via_api(
        cls,
        client: TestClient,
        **overrides: Any,
    ) -> dict[str, Any]:
        payload = cls.build_create(**overrides)
        resp = client.post("/api/v1/agents", json=payload)
        assert resp.status_code == 201, resp.text
        return resp.json()
