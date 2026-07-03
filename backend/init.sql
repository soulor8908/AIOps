-- AIOps Console — PostgreSQL 初始化脚本
-- 职责（specs/migration.spec.md §4）：仅创建扩展。无表依赖，可作为容器 initdb 脚本
-- 在应用/Alembic 之前执行。
--
-- 业务表结构、索引、种子数据均由 Alembic 迁移管理（migrations/versions/）。
-- 应用启动前请执行：alembic upgrade head

CREATE EXTENSION IF NOT EXISTS "vector";     -- pgvector 向量类型
CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "citext";     -- 大小写不敏感文本
