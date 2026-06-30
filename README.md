# AIOps Console

> AI原生运营控制台 | v0.1.0-alpha

AIOps Console 是一个 **AI 原生后台管理系统**，用于管理企业 AI 应用的全生命周期。

## 核心模块

- **Prompt Studio** — 版本化 Prompt 管理、A/B 测试、变量模板
- **Agent Orchestrator** — 多 Agent 编排、工作流 DAG、工具注册
- **Knowledge Base** — RAG 知识库、文档分块、向量检索、重排序
- **Model Router** — 多模型统一路由、负载均衡、Fallback 策略、成本监控
- **Conversation Analytics** — 对话质量分析、用户满意度、Token 消耗追踪
- **Eval Suite** — 自动化评估、LLM-as-judge、回归检测

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 |
| 前端 | Vue 3 · TypeScript · Vite · Pinia · Tailwind CSS |
| 数据库 | PostgreSQL 16 + pgvector · Redis 7 |
| AI 层 | 自研轻量 LLM 客户端（零框架） |

## 快速开始

```bash
# 启动开发环境（PostgreSQL + Redis + Backend + Frontend）
docker compose up -d

# 后端文档: http://localhost:8000/docs
# 前端界面: http://localhost:5173
```

## 项目结构

```
AIOps/
├── backend/          # FastAPI 后端
├── frontend/         # Vue 3 前端
├── specs/            # OpenAPI 3.1 规范
├── ops/              # 部署配置（Dockerfile, K8s）
├── docker-compose.yml
├── SPEC.md           # 项目总规格
└── agents.md         # Agent 工作流配置
```

## 开发工作流

遵循 **Spec → Eval → Code** 三段式：

1. 阅读相关 `SPEC.md`
2. 根据 SPEC 编写 eval（测试先于代码）
3. 实现代码，引用 SPEC 条款编号
4. 运行 eval（L1-L3 必须 100% 通过，L4 > 0.85）
5. 提交 PR（diff < 200 行）

## License

Proprietary — Internal use only.
