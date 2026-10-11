-- Monetize360 schema (ARCHITECTURE §16). Runs once on first Postgres start.

CREATE TABLE tenants (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tenant_policy (
    tenant_id           text PRIMARY KEY REFERENCES tenants(id),
    min_price           numeric(16,2),
    max_price           numeric(16,2),
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
    key_id           text,                                -- which API key asked for this price
    as_of            timestamptz NOT NULL,
    inputs           jsonb NOT NULL,
    signal_sources   jsonb NOT NULL,
    reference_price  numeric(16,2),                       -- max_change window anchor (ENGINE_SPEC §6.2)
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

-- What the max_change guard compares against, per priced thing (ENGINE_SPEC 6.2). In Postgres, not Redis:
-- losing an anchor would silently switch the guard off.
CREATE TABLE price_state (
    tenant_id     text NOT NULL REFERENCES tenants(id),
    domain        text NOT NULL,
    entity        text NOT NULL,
    anchor_price  numeric(16,2) NOT NULL,                 -- the price at the start of the current window
    anchor_time   timestamptz NOT NULL,
    last_price    numeric(16,2) NOT NULL,                 -- the most recent price (also the kill switch's "freeze")
    last_time     timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, domain, entity)
);

-- A retried request with the same idempotency_key gets the stored response instead of a second price.
CREATE TABLE idempotency_keys (
    tenant_id     text NOT NULL REFERENCES tenants(id),
    domain        text NOT NULL,
    key           text NOT NULL,
    request_hash  bytea NOT NULL,                          -- same key + another body = 409
    response      jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, domain, key)
);
CREATE INDEX idempotency_age_idx ON idempotency_keys (tenant_id, created_at);

-- ---------- permissions: app role can never rewrite history ----------
GRANT SELECT ON tenants TO m360_app;                           -- tenants are created by the operator (owner role)
GRANT SELECT, INSERT, UPDATE ON tenant_policy, pack_pointer TO m360_app;
GRANT SELECT, INSERT ON api_keys TO m360_app;
GRANT UPDATE (revoked_at) ON api_keys TO m360_app;            -- create and revoke; never rewrite secret, role or scope
GRANT SELECT, INSERT, UPDATE (status, approver, compiled, lint_report, verify_report) ON packs TO m360_app;
GRANT SELECT, INSERT ON audit_log TO m360_app;                -- no UPDATE, no DELETE
GRANT SELECT, INSERT, UPDATE ON price_state TO m360_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON idempotency_keys TO m360_app;   -- DELETE: expire after 24 h

-- ---------- Row-Level Security: second line of tenant isolation ----------
-- The app runs  SET LOCAL app.tenant_id = '<tenant from API key>'  in every transaction.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['tenant_policy', 'api_keys', 'packs', 'pack_pointer', 'audit_log', 'price_state',
                             'idempotency_keys'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = current_setting(''app.tenant_id'', true))',
            t);
    END LOOP;
END $$;

-- A tenant may read its own row only: no listing, renaming or creating other companies.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenants USING (id = current_setting('app.tenant_id', true));

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

-- Pricing replicas load EVERY tenant's live pack at startup, before any request (so before any tenant is known).
-- Only the service account can call this; a tenant's own queries still go through RLS.
CREATE FUNCTION list_live_packs()
RETURNS TABLE (tenant_id text, domain text, version integer, yaml text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
    SELECT p.tenant_id, p.domain, p.version, p.yaml
    FROM pack_pointer pp
    JOIN packs p ON p.tenant_id = pp.tenant_id AND p.domain = pp.domain AND p.version = pp.live_version
$$;
REVOKE ALL ON FUNCTION list_live_packs() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION list_live_packs() TO m360_app;
