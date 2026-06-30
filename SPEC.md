# AIOps Console — 项目总规格

> **版本**: v0.1.0-alpha | **日期**: 2026-06-30
> **哲学**: Agentic Engineering · Spec-Driven · Minimalism · Understanding-First

---

## 0. 架构哲学

遵循 Karpathy **Agentic Engineering** 哲学：

| 原则 | 实践 |
|------|------|
| **Spec即契约** | 每个模块先有 SPEC.md 再写测试再写代码 |
| **Minimalism** | 引入依赖前通过50行测试，能用50行内实现就不引入库 |
| **Understanding-First** | AI生成代码后必须能读懂，禁止黑盒依赖 |
| **Eval-Driven** | 每个功能必须有可自动验证的 eval |
| **Context as Code** | agents.md 和 SPEC.md 是 LLM 的上下文程序 |
| **Flat > Deep** | 目录扁平，模块自包含，避免深层抽象 |
| **Types as Docs** | Pydantic/TypeScript 类型即文档，零注释冗余 |

## 1. 项目定位

AIOps Console 是一个 **AI 原生后台管理系统**，用于管理企业 AI 应用的全生命周期。

### 1.1 核心模块

- **Prompt Studio** — 版本化 Prompt 管理、A/B 测试、变量模板
- **Agent Orchestrator** — 多 Agent 编排、工作流 DAG、工具注册
- **Knowledge Base** — RAG 知识库、文档分块、向量检索、重排序
- **Model Router** — 多模型统一路由、负载均衡、Fallback、成本监控
- **Conversation Analytics** — 对话质量分析、用户满意度、Token 追踪
- **Eval Suite** — 自动化评估、LLM-as-judge、回归检测

### 1.2 目标用户

- AI 应用开发团队（5-30 人）
- 需要管理多个 LLM 项目的技术负责人
- 需要 Prompt 版本控制和 A/B 测试的 AI 产品经理

### 1.3 非目标

- 不是通用低代码平台
- 不是模型训练平台（只做推理路由）
- 不是多租户 SaaS（单租户企业部署优先）

## 2. 技术选型

### 2.1 后端

| 技术 | 版本 | 理由 |
|------|------|------|
| Python | 3.12 | AI 生态原生 |
| FastAPI | 0.115 | 自动生成 OpenAPI，Pydantic v2 原生 |
| Pydantic v2 | 2.9 | Rust 核心，性能 10x，类型严格 |
| SQLAlchemy 2.0 | 2.0 | ORM 但不隐藏 SQL，支持 async |
| pgvector | 0.7 | PostgreSQL 向量扩展，无需额外数据库 |
| Redis | 7.2 | 缓存 + 消息队列 + 分布式锁 |
| pytest | 8.3 | 测试框架，async 支持完善 |

### 2.2 前端

| 技术 | 版本 | 理由 |
|------|------|------|
| Vue 3 | 3.5 | 响应式直觉友好，script setup 简洁 |
| TypeScript | 5.6 | 严格模式，与 Pydantic 类型可双向推导 |
| Vite | 8 | Rolldown 引擎，构建速度 10x |
| Pinia | 3 | 官方状态管理，Setup Store 语法简洁 |
| Tailwind CSS | 3.4 | 原子化 CSS，无需设计系统即可保持统一 |
| shadcn-vue | 0.11 | 可复制组件，非 npm 依赖，完全可控 |

### 2.3 AI 层

自研轻量客户端（约 80 行），直接封装 OpenAI/Anthropic API，零隐藏逻辑。

## 3. 目录结构

```
AIOps/
├── SPEC.md                          # 项目总规格
├── agents.md                        # Agent 工作流配置
├── docker-compose.yml               # 本地开发环境
├── backend/
│   ├── SPEC.md                      # 后端规格
│   ├── DEPENDENCY.md                # 依赖声明与理由
│   ├── pyproject.toml               # Poetry 配置
│   ├── Dockerfile
│   ├── init.sql                     # 数据库初始化
│   └── app/
│       ├── main.py                  # FastAPI 入口
│       ├── api.py                   # 路由聚合
│       ├── core/                    # 核心模块
│       └── domains/                 # 领域层（扁平自包含）
│           ├── prompts/
│           ├── agents/
│           ├── knowledge/
│           ├── models/
│           ├── analytics/
│           └── evals/
├── frontend/
│   ├── SPEC.md                      # 前端规格
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   └── src/
│       ├── domains/                 # 与后端 domains 严格对齐
│       ├── shared/                  # 共享层
│       └── views/                   # 页面
├── specs/
│   └── openapi.yaml                 # OpenAPI 3.1 规范
└── ops/
    ├── Dockerfile.backend
    ├── Dockerfile.frontend
    └── k8s/                         # K8s manifests
```

## 4. 核心领域概要

详见各领域 `SPEC.md` 及 `specs/openapi.yaml`。

| 领域 | 核心能力 | 关键约束 |
|------|---------|---------|
| Prompt Studio | CRUD + 版本管理 + 回滚 + diff | 单 Prompt 最大 100 版本，内容 64KB |
| Agent Orchestrator | Agent 定义 + DAG 编排 + 执行追踪 | DAG 最大 50 节点，单次最大 10 轮 |
| Knowledge Base | 文档上传 + 分块 + 向量检索 + RAG | 单文档 50MB，向量维度 1536 |
| Model Router | 多模型配置 + 路由 + Fallback + 成本 | 路由策略：direct/round_robin/least_cost/latency |
| Conversation Analytics | 对话记录 + Token 统计 + 质量评分 | 按用户/模型/时间维度统计 |
| Eval Suite | 规则测试 + LLM-as-judge + 回归检测 | L4 得分 > 0.85 |

## 5. 开发工作流

**Spec → Eval → Code** 三段式：

1. **Spec 定义** — 产出 SPEC.md、API.spec.yaml、DATA.spec.md
2. **Eval 编写** — L1 单元 / L2 契约 / L3 E2E / L4 LLM-as-Judge
3. **Code 实现** — 引用 Spec 条款，eval 通过，diff < 200 行

## 6. 部署

- **本地开发**：docker compose up（PostgreSQL + Redis + Backend + Frontend）
- **生产部署**：K8s 极简 manifest（见 ops/k8s/）

---

> 本 spec 由 Agentic Engineering 流程维护。
> 每次架构变更必须先更新 SPEC.md，再更新 eval，再更新代码。
