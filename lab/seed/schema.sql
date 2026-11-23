-- OpsPilot Lab Schema Definition
-- Creates products, inventory, orders, and deploys tables.

CREATE TABLE IF NOT EXISTS products (
    id BIGSERIAL PRIMARY KEY,
    sku VARCHAR(64) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0)
);

CREATE TABLE IF NOT EXISTS inventory (
    product_id BIGINT PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    qty INTEGER NOT NULL CHECK (qty >= 0)
);

CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    status VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Schema and table for Phase 1.8 deployment tracking
CREATE SCHEMA IF NOT EXISTS lab;

CREATE TABLE IF NOT EXISTS lab.deploys (
    deploy_id VARCHAR(64) PRIMARY KEY DEFAULT ('dep-' || substr(md5(random()::text || clock_timestamp()::text), 1, 12)),
    service VARCHAR(64) NOT NULL,
    old_version VARCHAR(32) NOT NULL DEFAULT '',
    new_version VARCHAR(32) NOT NULL DEFAULT '',
    deployed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    author VARCHAR(128) NOT NULL DEFAULT 'synthetic',
    summary TEXT NOT NULL DEFAULT '',
    changed_files JSONB NOT NULL DEFAULT '[]'::jsonb,
    config_changes JSONB NOT NULL DEFAULT '{}'::jsonb,
    diff_text TEXT NOT NULL DEFAULT '',
    version VARCHAR(32) DEFAULT 'v1.0.0',
    commit_sha VARCHAR(64) DEFAULT 'synthetic',
    deployed_by VARCHAR(128) DEFAULT 'synthetic',
    status VARCHAR(32) DEFAULT 'SUCCESS'
);

-- Updatable view in public schema for backward compatibility and unqualified queries
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'deploys') THEN
        DROP TABLE public.deploys CASCADE;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_views WHERE schemaname = 'public' AND viewname = 'deploys') THEN
        CREATE VIEW public.deploys AS SELECT * FROM lab.deploys;
    END IF;
END $$;

-- Indexes for query efficiency
CREATE INDEX IF NOT EXISTS idx_orders_product_id ON orders(product_id);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_deploys_service_deployed_at ON lab.deploys(service, deployed_at);
