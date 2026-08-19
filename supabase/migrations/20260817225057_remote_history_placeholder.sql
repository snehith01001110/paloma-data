-- Production already records migration 20260817225057 as applied, but its original file was
-- not retained in this repository. This no-op preserves migration-history alignment without
-- replaying or reverting unknown production DDL. The current schema is governed by later,
-- idempotent migrations and verified after deployment.

