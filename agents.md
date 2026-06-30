# AIOps Console - Agent 工作流配置

> Karpathy 风格：agents.md 是 LLM 的上下文程序。
> 每个 Agent 必须理解此文件，才能参与本项目的开发。

## 1. 项目元信息

- **名称**：AIOps Console
- **语言**：Python 3.12 (backend) / TypeScript 5.6 (frontend)
- **框架**：FastAPI · Vue 3 · SQLAlchemy 2.0 · Pinia 3
- **风格**：Minimalism · Flat structure · Understanding-first

## 2. 开发工作流（强制）

```
1. 阅读相关 SPEC.md
2. 根据 SPEC 编写 eval（测试先于代码）
3. 实现代码，引用 SPEC 条款编号
4. 运行 eval（L1-L3 必须 100% 通过，L4 > 0.85）
5. 提交 PR（diff < 200 行，大 feature 拆分）
```

## 3. 代码风格规则

### Python
- 使用函数式代码，纯函数优先
- 类型注解必须完整，禁止 Any（除非确实任意）
- 函数长度 < 50 行，超过必须拆分
- 注释只解释"为什么"，不解释"做什么"，代码自解释
- 异常处理：显式抛出，不吞异常

### TypeScript / Vue
- 严格模式开启，禁止 any
- 使用 script setup + 组合式 API
- 组件 < 150 行，超过拆分
- API 层使用生成的类型，禁止手写接口定义

## 4. 依赖决策树

```
需要这个功能
  能用50行以内实现 → 自己写
  不能 → 它有多少 transitive dependencies
       <5 → 可以考虑
       >20 → 红旗，找替代或 yoink 核心代码
       必须引入 → 在 DEPENDENCY.md 中说明理由
```

## 5. 禁止模式

- 禁止引入 LangChain/LangGraph，自研 LLM 客户端
- 禁止引入 ORM 之上的 DAO 层，直接用 SQLAlchemy
- 禁止手写 API 类型，从 OpenAPI 生成
- 禁止深层目录嵌套（max 3 层）
- 禁止在 Store 中直接调用 API，Store 只管理状态

## 6. Eval 规范

每个功能必须有：
- **L1** pytest 单元测试，覆盖率 > 80%
- **L2** schemathesis 契约测试
- **L3** Playwright E2E 测试（关键路径）
- **L4** LLM-as-judge 语义质量

## 7. Commit Message 格式

```
<domain> <action> <target>

- 引用SPEC <spec-file>#<clause>
- Eval <eval-file> 通过
```

Example:
```
prompts add version rollback

- 引用SPEC SPEC.md#5.1
- Eval eval_prompts.py 通过 L1-L4
```

## 8. 多 Agent 协作模式

当使用多个 Agent 并行开发时：
- **Agent A** 负责 API 层（router + models）
- **Agent B** 负责业务逻辑（service）
- **Agent C** 负责 eval 和测试
- **人工** 负责 review diff 和合并

每个 Agent 的上下文必须包含：
1. 本 agents.md
2. 相关 SPEC.md
3. 已生成的 OpenAPI spec（确保类型一致）
