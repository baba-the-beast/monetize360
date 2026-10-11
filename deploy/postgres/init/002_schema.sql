-- Monetize360 schema (ARCHITECTURE §16). Runs once on first Postgres start.

CREATE TABLE tenants (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tenant_policy (
    tenant_id           text PRIMARY KEY REFERENCES tenants(id),
    min_price           numeric(14,2),
    max_price           numeric(14,2),
    max_surge_x         numeric(6,2),
    banned_signals      text[] NOT NULL DEFAULT '{}',
    require_two_person  boolean NOT NULL DEFAULT true,
    kill_mode           text NOT NULL DEFAULT 'normal'
                        CHECK (kill_mode IN ('normal', 'freeze', 'base_only')),
    CHECK (min_price IS NULL OR max_price IS NULL OR min_price <= max_price)
);

CREATE TABLE api_keys (
    key_id          text PRIMARY KEY,
    tenant_id       text NOT NULL REFERENCES tenants(id),
    secret_sha256   bytea NOT NULL,
    role            text NOT NULL CHECK (role IN ('pricing', 'editor', 'approver', 'admin')),
    domains         text[] NOT NULL,
    rate_limit_rps  integer NOT NULL DEFAULT 200,
    expires_at      timestamptz,
    revoked_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE packs (
    tenant_id      text NOT NULL REFERENCES tenants(id),
    domain         text NOT NULL,
    version        integer NOT NULL,
    status         text NOT NULL CHECK (status IN ('draft', 'approved', 'published', 'archived')),
    yaml           text NOT NULL,
    compiled       jsonb,
    owner          text NOT NULL,
    approver       text,
    lint_report    jsonb,
    verify_report  jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, domain, version),
    CHECK (approver IS NULL OR approver <> owner)          -- two-person rule
);

CREATE TABLE pack_pointer (                                -- what "live" means; rollback = UPDATE this
    tenant_id     text NOT NULL,
    domain        text NOT NULL,
    live_version  integer NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, domain),
    FOREIGN KEY (tenant_id, domain, live_version) REFERENCES packs(tenant_id, domain, version)
);

CREATE TABLE audit_log (
    price_id         text NOT NULL,
    tenant_id        text NOT NULL,
    domain           text NOT NULL,
    seq              bigint NOT NULL,                     -- position in the (tenant, domain) chain
    pack_version     integer NOT NULL,
    entity           text,
    as_of            timestamptz NOT NULL,
    inputs           jsonb NOT NULL,
    signal_sources   jsonb NOT NULL,
    reference_price  numeric(14,2),                       -- max_change window anchor (ENGINE_SPEC §6.2)
    output           jsonb NOT NULL,
    prev_hash        bytea NOT NULL,
    hash             bytea NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (price_id, created_at)
) PARTITION BY RANGE (created_at);

-- ponytail: a few fixed monthly partitions + default; production creates them on a schedule (pg_partman, free)
CREATE TABLE audit_log_2026_10 PARTITION OF audit_log FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE audit_log_2026_11 PARTITION OF audit_log FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE audit_log_2026_12 PARTITION OF audit_log FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');
CREATE TABLE audit_log_default PARTITION OF audit_log DEFAULT;

CREATE INDEX audit_chain_idx ON audit_log (tenant_id, domain, seq);

-- ---------- permissions: app role can never rewrite history ----------
GRANT SELECT, INSERT, UPDATE ON tenants, tenant_policy, api_keys, pack_pointer TO m360_app;
GRANT SELECT, INSERT, UPDATE (status, approver, compiled, lint_report, verify_report) ON packs TO m360_app;
GRANT SELECT, INSERT ON audit_log TO m360_app;                -- no UPDATE, no DELETE

-- ---------- Row-Level Security: second line of tenant isolation ----------
-- The app runs  SET LOCAL app.tenant_id = '<tenant from API key>'  in every transaction.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['tenant_policy', 'api_keys', 'packs', 'pack_pointer', 'audit_log'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true))',
            t);
    END LOOP;
END $$;

-- Auth runs BEFORE the tenant is known, so RLS would hide every key.
-- This one narrow function (runs as the table owner) returns a single key by id - nothing else.
CREATE FUNCTION lookup_api_key(p_key_id text)
RETURNS TABLE (key_id text, tenant_id text, secret_sha256 bytea, role text, domains text[],
               rate_limit_rps integer, expires_at timestamptz, revoked_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT key_id, tenant_id, secret_sha256, role, domains, rate_limit_rps, expires_at, revoked_at
    FROM api_keys WHERE api_keys.key_id = p_key_id
$$;
REVOKE ALL ON FUNCTION lookup_api_key(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lookup_api_key(text) TO m360_app;
