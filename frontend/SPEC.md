# AIOps Console Frontend Spec

> Version: v0.1.0-alpha | Date: 2026-06-30
> Philosophy: Agentic Engineering · Spec-Driven · Minimalism · Understanding-First

---

## 1. 目标

AIOps Console 前端是 AI 原生后台管理系统的 Web 控制台。它消费后端 FastAPI 生成的 OpenAPI 3.1 规范，提供六大领域（Prompt Studio、Agent Orchestrator、Knowledge Base、Model Router、Conversation Analytics、Eval Suite）的可视化管理能力。

## 2. 技术选型

| 技术 | 版本 | 理由 |
|------|------|------|
| Vue 3 | 3.5 | 响应式直觉友好，script setup 简洁 |
| TypeScript | 5.6 | 严格模式，与 Pydantic 类型可双向推导 |
| Vite | 8 | Rolldown 引擎，构建速度 10x |
| Pinia | 3 | 官方状态管理，Setup Store 语法简洁 |
| Vue Router | 4 | 官方路由，与后端 API 路径对齐 |
| Tailwind CSS | 3.4 | 原子化 CSS，无需设计系统即可保持统一 |
| shadcn-vue | 0.11 风格 | 可复制组件，非 npm 依赖，完全可控 |
| openapi-typescript | 7 | 从 OpenAPI spec 生成 TypeScript 类型 |

## 3. 目录结构

```
frontend/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── tsconfig.node.json
├── tailwind.config.js
├── postcss.config.js
├── index.html
├── env.d.ts
└── src/
    ├── main.ts                # Vue 应用入口
    ├── App.vue                # 根组件（layout：侧边栏 + 主内容）
    ├── router.ts              # Vue Router 配置
    ├── style.css              # Tailwind 基础样式 + CSS 变量
    ├── shared/                # 共享层
    │   ├── api/
    │   │   ├── client.ts      # 基于 fetch 的 API 客户端（~50 行）
    │   │   └── types.ts       # OpenAPI 生成的类型
    │   ├── ui/                # shadcn-vue 风格 UI 组件
    │   ├── stores/            # 全局 store（app, user）
    │   └── utils/             # 纯工具函数
    ├── domains/               # 领域层（与后端 domains 严格对齐）
    │   ├── prompts/
    │   ├── agents/
    │   ├── knowledge/
    │   ├── models/
    │   ├── analytics/
    │   └── evals/
    └── views/                 # 页面视图（薄层，只组装 domain 组件）
```

## 4. 架构原则

### 4.1 Spec 即契约
- API 类型从 OpenAPI 生成（`npm run gen:api`），手动维护作为兜底。
- 类型即文档，零冗余注释。

### 4.2 Minimalism
- API 客户端约 50 行，基于 fetch，零依赖，无 axios 黑盒。
- 引入依赖前先用 50 行测试自证可行。

### 4.3 Flat > Deep
- 目录扁平，每个领域自包含：`api.ts` + `store.ts` + `components/`。
- 页面视图（`views/`）是薄层，只组装 domain 组件，不含业务逻辑。

### 4.4 关注点分离
- `api.ts`：所有 HTTP 调用，使用 `shared/api/client.ts`。
- `store.ts`：Pinia Setup Store，只管理状态与编排 API 调用。
- `components/`：纯 UI，通过 store 读写状态。

### 4.5 严格 TypeScript
- `strict: true`，禁止 `any`。
- 所有跨边界数据使用 `shared/api/types.ts` 中的类型。

## 5. 路由设计

| Path | View | Domain |
|------|------|--------|
| `/` | DashboardView | analytics |
| `/prompts` | PromptsView | prompts |
| `/agents` | AgentsView | agents |
| `/knowledge` | KnowledgeView | knowledge |
| `/models` | ModelsView | models |
| `/analytics` | AnalyticsView | analytics |
| `/evals` | EvalsView | evals |

## 6. UI 设计

- shadcn-vue 风格，使用 CSS 变量驱动主题（background / foreground / primary / muted / border 等）。
- Tailwind 原子化样式，组件 < 150 行。
- 侧边栏导航 + 主内容区 layout，支持折叠。

## 7. 开发工作流

```bash
npm install           # 安装依赖
npm run gen:api       # 从 OpenAPI 生成类型（可选，已有兜底类型）
npm run dev           # 启动开发服务器（代理 /api -> localhost:8000）
npm run type-check    # 类型检查
npm run build         # 生产构建
npm run preview       # 预览生产构建
```

---

> 本 spec 由 Agentic Engineering 流程维护。
> 每次架构变更必须先更新 SPEC.md，再更新 eval，再更新代码。
