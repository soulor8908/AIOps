.PHONY: help install dev test lint type-check e2e build gen-api migrate docker-build

help: ## 显示可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## 安装前后端依赖
	cd backend && pip install -e .
	cd frontend && npm ci

dev: ## 启动开发服务器（需要 docker compose up db redis）
	cd backend && uvicorn app.main:app --reload --port 8000 &
	cd frontend && npm run dev

test: ## 运行所有测试
	cd backend && pytest
	cd frontend && npm run test:run

lint: ## 代码检查
	cd backend && ruff check app tests && mypy app
	cd frontend && npm run type-check

e2e: ## 端到端测试
	cd frontend && npm run e2e

build: ## 构建前端产物
	cd frontend && npm run build

gen-api: ## 从 OpenAPI 生成前端类型
	cd frontend && npm run gen:api

migrate: ## 数据库迁移
	cd backend && alembic upgrade head

docker-build: ## 构建 Docker 镜像
	docker build -f ops/Dockerfile.backend -t aiops/backend:0.1.0 .
	docker build -f ops/Dockerfile.frontend -t aiops/frontend:0.1.0 .
