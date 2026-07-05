"""应用配置 — Pydantic Settings 单例。

环境变量优先级高于默认值。生产部署通过 .env 或 K8s ConfigMap/Secret 注入。
字段命名遵循 `specs/security.spec.md`§2（`JWT_SECRET` / `JWT_EXPIRE_HOURS`）
与 `specs/observability.spec.md`§3（`LOG_LEVEL`）。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 默认占位密钥：仅允许在开发/测试环境使用，生产环境必须通过环境变量显式覆盖。
_INSECURE_DEFAULT_SECRET = "change-me"
# security.spec.md§2.3：JWT_SECRET 最小长度 32 字节。
_MIN_SECRET_BYTES = 32
# 生产环境标识：以下 environment 值触发 fail-fast 校验。
_PROD_ENVS = {"production", "prod", "staging"}


def _default_cors_origins() -> list[str]:
    """开发环境默认放宽到本地前端；生产须通过环境变量显式覆盖。

    `security.spec.md`§4 禁止生产环境 ``allow_origins=["*"]`` + ``allow_credentials=True``。

    同时放行 ``localhost`` 与 ``127.0.0.1``：浏览器视二者为不同 Origin，
    用户用 127.0.0.1 访问 dev server 时若不在白名单会被 CORS 拦截。
    同源部署（dev Vite proxy / prod nginx 反代）默认不触发 CORS，
    此列表仅覆盖前端直连后端 8000 端口的场景。
    """
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


class Settings(BaseSettings):
    """全局配置，所有字段从环境变量加载。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # 允许通过字段名（而非仅 alias）作为 init kwarg 传入，
        # 例如 Settings(environment="production")。否则 kwargs 会被当成 extra
        # 字段并因 extra="ignore" 被静默丢弃，导致 validator 拿不到 production。
        populate_by_name=True,
    )

    # 基础
    app_version: str = Field(default="0.1.0", alias="APP_VERSION")
    environment: str = Field(default="development", alias="ENVIRONMENT")
    debug: bool = Field(default=False, alias="DEBUG")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # 数据库 / 缓存
    database_url: str = Field(
        default="postgresql+asyncpg://aiops:aiops@localhost:5432/aiops",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379", alias="REDIS_URL")

    # 安全 — JWT
    # `JWT_SECRET` 为真源（`security.spec.md`§2.3），`SECRET_KEY` 作为兼容别名。
    jwt_secret: str = Field(default=_INSECURE_DEFAULT_SECRET, alias="JWT_SECRET")
    secret_key: str = Field(default=_INSECURE_DEFAULT_SECRET, alias="SECRET_KEY")
    jwt_expire_hours: int = Field(default=24, alias="JWT_EXPIRE_HOURS")
    # 兼容旧字段：若显式设置则覆盖 hours 计算；默认 None 表示由 hours 派生。
    access_token_expire_minutes: int | None = Field(
        default=None, alias="ACCESS_TOKEN_EXPIRE_MINUTES"
    )
    refresh_token_expire_days: int = Field(default=7, alias="REFRESH_TOKEN_EXPIRE_DAYS")
    cors_origins: list[str] = Field(default_factory=_default_cors_origins, alias="CORS_ORIGINS")
    # P0-1: token 黑名单开关。默认 True 启用,测试环境可关。Redis 不可用时
    # 自动降级放行(见 token_blacklist.is_revoked 注释)。
    token_blacklist_enabled: bool = Field(default=True, alias="TOKEN_BLACKLIST_ENABLED")
    # P0-2: 登录失败锁定(security.spec.md§6)。连续失败 N 次锁定 M 分钟。
    # Redis 不可用时降级放行(见 login_lockout 注释)。
    login_max_failures: int = Field(default=5, alias="LOGIN_MAX_FAILURES", ge=1, le=20)
    login_lockout_minutes: int = Field(default=15, alias="LOGIN_LOCKOUT_MINUTES", ge=1, le=1440)
    # P0-10：fire-and-forget task 背压上限。``asyncio.create_task`` 启动的
    # 在线 eval 采样 / 失败聚类记录等后台 task 经 TaskRegistry 统一管理，
    # 超过此并发数时丢弃新 task（仅记 warning）。0 = 不限制（仅持有强引用
    # 防 GC，不限并发——与历史行为一致，单测/CI 默认走此路径）。
    # 生产建议配置 50~200，按 pod 资源与 QPS 调整。
    task_registry_max_concurrency: int = Field(
        default=0, alias="TASK_REGISTRY_MAX_CONCURRENCY", ge=0, le=10000
    )

    # LLM 默认
    default_llm_provider: str = Field(default="openai", alias="DEFAULT_LLM_PROVIDER")
    default_llm_model: str = Field(default="gpt-4o-mini", alias="DEFAULT_LLM_MODEL")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    # P0-2：Agent autonomous loop worker。默认关闭，单测/CI 不启动 worker。
    # 启用后 lifespan 创建后台 task 周期扫描到期 agent 并并发执行。
    agent_scheduler_enabled: bool = Field(default=False, alias="AGENT_SCHEDULER_ENABLED")
    agent_scheduler_interval_seconds: int = Field(
        default=60, alias="AGENT_SCHEDULER_INTERVAL_SECONDS"
    )
    agent_scheduler_concurrency: int = Field(
        default=5, alias="AGENT_SCHEDULER_CONCURRENCY"
    )
    agent_scheduler_timeout_seconds: int = Field(
        default=300, alias="AGENT_SCHEDULER_TIMEOUT_SECONDS"
    )
    # P0-3：online eval 生产采样率。0.0 = 关闭（默认，单测/CI 不采样）。
    # 0~1 之间的概率，每次 execute_agent 成功后按此概率异步记录样本。
    # 采样用 asyncio.create_task fire-and-forget，不阻塞请求响应。
    online_eval_sample_rate: float = Field(
        default=0.0, alias="ONLINE_EVAL_SAMPLE_RATE", ge=0.0, le=1.0
    )
    # C5：分层采样——高优先级样本（长输入 / self-heal 触发 / 低 eval_score）的
    # 采样率倍数。effective_rate = min(base_rate * boost, 1.0)。默认 5.0 意味着
    # priority>0 的请求采样率放大 5 倍，确保稀有但易暴露回归的流量不被均匀采样稀释。
    online_eval_sample_rate_boost: float = Field(
        default=5.0, alias="ONLINE_EVAL_SAMPLE_RATE_BOOST", ge=1.0, le=100.0
    )
    # C5：priority 启发式阈值——输入字符数超过此值则 priority +1（长查询更可能
    # 触发上下文压缩 / 多轮工具调用，回归价值高）。
    online_eval_priority_input_len_threshold: int = Field(
        default=200, alias="ONLINE_EVAL_PRIORITY_INPUT_LEN_THRESHOLD", ge=1
    )
    # P1-4：Agent 记忆层。默认关闭，单测/CI 不启用（避免 pgvector 依赖）。
    # 启用后 execute_agent 构造 PgMemoryBackend 传入 executor，执行前检索
    # top-k 相关历史注入 context，每轮结束后持久化 observation/final_answer。
    agent_memory_enabled: bool = Field(default=False, alias="AGENT_MEMORY_ENABLED")
    agent_memory_top_k: int = Field(default=3, alias="AGENT_MEMORY_TOP_K", ge=1, le=20)
    # P1-5：查询改写 / HyDE。默认关闭。启用后 execute_agent 用 LLM 改写 query
    # 生成变体 + HyDE 假设文档，多 query 并发检索记忆去重。
    agent_query_rewrite_enabled: bool = Field(
        default=False, alias="AGENT_QUERY_REWRITE_ENABLED"
    )
    agent_query_rewrite_n_variants: int = Field(
        default=2, alias="AGENT_QUERY_REWRITE_N_VARIANTS", ge=0, le=5
    )
    agent_query_rewrite_hyde: bool = Field(
        default=True, alias="AGENT_QUERY_REWRITE_HYDE"
    )
    # P1-6：成本感知模型路由。默认关闭。启用后按任务复杂度路由到
    # cheap/default/premium 模型，token budget 超限熔断降级到 cheapest。
    agent_cost_routing_enabled: bool = Field(
        default=False, alias="AGENT_COST_ROUTING_ENABLED"
    )
    agent_cost_cheap_model_alias: str = Field(
        default="cheap", alias="AGENT_COST_CHEAP_MODEL_ALIAS"
    )
    agent_cost_premium_model_alias: str = Field(
        default="premium", alias="AGENT_COST_PREMIUM_MODEL_ALIAS"
    )
    # token budget 滑动窗口（0 = 不限制）。默认 0，生产按需配置。
    agent_cost_token_budget: int = Field(
        default=0, alias="AGENT_COST_TOKEN_BUDGET", ge=0
    )
    agent_cost_budget_window_seconds: int = Field(
        default=3600, alias="AGENT_COST_BUDGET_WINDOW_SECONDS", ge=1
    )
    # A1：多副本共享预算。启用后 BudgetTracker 走 Redis ZSET 实现，所有 pod
    # 共享同一预算视图，熔断判定基于全局真实消耗。默认关闭（与历史行为一致，
    # 单测/CI 走内存版）。生产部署必须显式启用——K8s HPA 多副本下内存版预算
    # 共享失效，熔断永远不触发，成本失控。
    agent_cost_budget_redis_enabled: bool = Field(
        default=False, alias="AGENT_COST_BUDGET_REDIS_ENABLED"
    )
    # A2：code 工具执行开关。ToolType.code 暴露给 LLM 生成任意代码并通过 tool_call
    # 触发执行——是脚枪。默认 False（executor 拒绝 code 工具并跳过 schema 注入），
    # 仅当显式启用且 tool_executor 注入了沙箱化执行器时才允许。
    agent_code_tool_enabled: bool = Field(
        default=False, alias="AGENT_CODE_TOOL_ENABLED"
    )
    # P2-8：失败模式聚类。默认关闭。启用后 executor 把工具/LLM 失败的 error
    # message 向量化存入 FailureClusterer，可通过 /agents/failure-clusters 查看。
    agent_failure_clustering_enabled: bool = Field(
        default=False, alias="AGENT_FAILURE_CLUSTERING_ENABLED"
    )
    agent_failure_cluster_distance_threshold: float = Field(
        default=0.3, alias="AGENT_FAILURE_CLUSTER_DISTANCE_THRESHOLD", ge=0.0, le=2.0
    )
    # C6：FailureClusterer SQLite DLQ 路径。空字符串 = 不启用 DLQ（纯内存，
    # 与历史行为一致）。``:memory:`` = 进程内临时库（单测用）。文件路径 = 持久化
    # 到磁盘，进程重启后可 replay 未 embed 的记录。生产建议配置文件路径。
    agent_failure_cluster_dlq_path: str = Field(
        default="", alias="AGENT_FAILURE_CLUSTER_DLQ_PATH"
    )
    # P2-10：Planning + Reflection。默认关闭。启用后 execute_agent 在 ReAct
    # 循环前用 LLM 生成执行计划注入 system 消息，循环后用 LLM 对照 plan + traces
    # 产出结构化反思存入 ExecutionResult。两者独立可单独启用。
    agent_planning_enabled: bool = Field(
        default=False, alias="AGENT_PLANNING_ENABLED"
    )
    agent_reflection_enabled: bool = Field(
        default=False, alias="AGENT_REFLECTION_ENABLED"
    )
    # B1：单次执行最大轮次上限。默认 10（保守，避免失控循环），可配到 50
    # （长任务 Agent 如 deep research 需要更多轮）。AgentCreate.max_turns 与
    # ExecuteRequest.max_turns 的 le 上限用此值动态校验（service 层 validator），
    # Pydantic schema 用绝对上限 50 兜底防滥用。
    agent_max_turns: int = Field(
        default=10, alias="AGENT_MAX_TURNS", ge=1, le=50
    )
    # B4：context 压缩阈值（token 数）。ReAct 循环累积消息超此阈值时用 LLM
    # 摘要历史，避免超 context window。默认 4000（约 16K 字符），可按模型调优。
    # 压缩触发时记入 traces（thought 标注 context_compressed）。
    agent_context_compress_tokens: int = Field(
        default=4000, alias="AGENT_CONTEXT_COMPRESS_TOKENS", ge=500, le=100000
    )
    # B3：self-eval 采样次数。LLM judge 有 ±0.1 噪声，单次采样 0.85 阈值无统计
    # 意义。默认 3 次采样取均值（成本×3 但阈值判定可靠）。1 = 退化为单次
    # （向后兼容，成本敏感场景可配 1）。
    agent_self_eval_samples: int = Field(
        default=3, alias="AGENT_SELF_EVAL_SAMPLES", ge=1, le=10
    )

    @property
    def access_token_expire_seconds(self) -> int:
        """access token 实际过期秒数。

        优先使用 `ACCESS_TOKEN_EXPIRE_MINUTES`（向后兼容），否则由
        `JWT_EXPIRE_HOURS` 派生（默认 24h，遵循 `security.spec.md`§2.2）。
        """
        if self.access_token_expire_minutes is not None:
            return self.access_token_expire_minutes * 60
        return self.jwt_expire_hours * 3600

    @property
    def refresh_token_expire_seconds(self) -> int:
        """refresh token 过期秒数（默认 7d）。"""
        return self.refresh_token_expire_days * 86400

    @property
    def effective_secret_key(self) -> str:
        """返回实际使用的 JWT 签名密钥（JWT_SECRET 优先于 SECRET_KEY）。"""
        # 若 JWT_SECRET 被显式覆盖（非默认值）则用它，否则回退到 SECRET_KEY
        return (
            self.jwt_secret
            if self.jwt_secret != _INSECURE_DEFAULT_SECRET
            else self.secret_key
        )

    @model_validator(mode="after")
    def _validate_secret_key(self) -> Settings:
        """生产环境 fail-fast：拒绝默认占位密钥与过短密钥（security.spec.md§2.3/§9）。

        - 开发/测试环境允许使用占位符 ``change-me`` 以降低本地启动门槛。
        - 生产/staging 环境必须通过环境变量（或 K8s Secret）显式注入
          长度 ≥ 32 字节的强密钥，否则启动直接报错，杜绝弱密钥上生产。
        """
        if self.environment.lower() not in _PROD_ENVS:
            return self
        secret = self.effective_secret_key
        if secret == _INSECURE_DEFAULT_SECRET:
            raise ValueError(
                "JWT_SECRET 必须在生产环境通过环境变量显式设置，"
                "禁止使用默认占位值 'change-me'（security.spec.md§2.3/§9）"
            )
        if len(secret.encode("utf-8")) < _MIN_SECRET_BYTES:
            raise ValueError(
                f"JWT_SECRET 长度不足 {_MIN_SECRET_BYTES} 字节"
                f"（当前 {len(secret.encode('utf-8'))} 字节），"
                "security.spec.md§2.3 要求最小 32 字节"
            )
        # 生产环境禁止 debug=True：Starlette ServerErrorMiddleware 在 debug=True 时
        # 返回明文 traceback，违反 errors.spec.md§5.4（禁止泄漏 str(exc)）。
        if self.debug:
            raise ValueError(
                "生产环境必须 DEBUG=false（errors.spec.md§5.4）："
                "debug=True 会导致异常时返回明文 traceback 泄漏内部信息"
            )
        return self

    @model_validator(mode="after")
    def _validate_cors(self) -> Settings:
        """CORS 安全校验（security.spec.md§4）。

        禁止 ``allow_origins=["*"]`` 与 ``allow_credentials=True`` 同时出现
        （浏览器规范亦不允许，且是高危误配）。main.py 中 ``allow_credentials=True``
        硬编码，因此 ``cors_origins`` 不得含 ``"*"``——必须显式指定 Origin 列表。
        """
        if "*" in self.cors_origins:
            raise ValueError(
                "CORS_ORIGINS 不得包含 '*'（security.spec.md§4）："
                "allow_credentials=True 与通配 Origin 不可同时使用，"
                "请显式指定允许的 Origin 列表（如 ['https://console.example.com']）"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """返回缓存的单例 Settings。"""
    return Settings()


settings: Settings = get_settings()
