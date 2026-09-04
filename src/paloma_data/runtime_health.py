from __future__ import annotations

from typing import Any


LIVE_DETAILS_SOURCE_COLUMNS = frozenset(
    {
        "consumer_facing",
        "public_access",
        "quality_flags",
        "retired_at",
        "source",
        "source_record_id",
        "source_status",
    }
)


def live_details_runtime_health(conn: Any) -> dict[str, Any]:
    """Check the database contract used before a live provider request.

    This intentionally does not fetch provider attributes or require a consumer
    account.  It catches privilege, RLS-policy, and linked-record regressions in
    the private eligibility path while keeping provider payload columns denied.
    """
    security = conn.execute(
        """
        select
          coalesce((
            select relrowsecurity
            from pg_catalog.pg_class relation
            join pg_catalog.pg_namespace namespace
              on namespace.oid = relation.relnamespace
            where namespace.nspname = 'ingest'
              and relation.relname = 'source_records'
          ), false) as rls_enabled,
          exists (
            select 1
            from pg_catalog.pg_policies policy
            where policy.schemaname = 'ingest'
              and policy.tablename = 'source_records'
              and policy.policyname =
                'paloma_runtime_read_eligible_fsq_source_records'
              and policy.cmd = 'SELECT'
              and 'paloma_runtime'::name = any(policy.roles)
          ) as runtime_policy_enabled,
          coalesce((
            select array_agg(column_name::text order by column_name)
            from information_schema.columns
            where table_schema = 'ingest'
              and table_name = 'source_records'
              and has_column_privilege(
                'paloma_runtime',
                'ingest.source_records',
                column_name,
                'SELECT'
              )
          ), array[]::text[]) as readable_columns
        """
    ).fetchone()
    coverage = conn.execute(
        """
        select
          count(*) filter (
            where establishment.publication_state = 'published'
              and establishment.status = 'open'
              and establishment.access_mode = 'walk_in'
              and establishment.verification_tier
                in ('open_evidence', 'provider', 'manual')
              and establishment.verification_expires_at > now()
              and candidate.candidate_state in ('verified', 'published')
              and candidate.identity_confidence >= 0.96
          ) as expected_publications,
          count(*) filter (
            where establishment.publication_state = 'published'
              and establishment.status = 'open'
              and establishment.access_mode = 'walk_in'
              and establishment.verification_tier
                in ('open_evidence', 'provider', 'manual')
              and establishment.verification_expires_at > now()
              and candidate.candidate_state in ('verified', 'published')
              and candidate.identity_confidence >= 0.96
              and exists (
                select 1
                from ingest.candidate_source_links link
                left join ingest.source_records source_record
                  on source_record.source = link.source
                 and source_record.source_record_id = link.source_record_id
                where link.candidate_id = candidate.id
                  and link.source = 'fsq'
                  and link.identity_confidence >= 0.96
                  and (
                    (
                      source_record.source_record_id is not null
                      and source_record.retired_at is null
                      and source_record.source_status = 'open'
                      and source_record.consumer_facing
                      and source_record.public_access = 'walk_in'
                      and not (source_record.quality_flags && array[
                        'closed', 'delete', 'doesnt_exist', 'does_not_exist',
                        'duplicate', 'inappropriate', 'privatevenue', 'private_venue'
                      ]::text[])
                    )
                    or (
                      link.match_method =
                        'reviewed_identity_exception:anchor_source_id'
                      and candidate.anchor_source = 'fsq'
                      and candidate.anchor_source_record_id = link.source_record_id
                      and establishment.verification_tier = 'manual'
                    )
                  )
              )
          ) as eligible_publications,
          count(*) filter (
            where establishment.publication_state = 'published'
              and establishment.status = 'open'
              and (establishment.hours is null or establishment.price_level is null)
          ) as publications_needing_live_hours_or_price
        from public.establishments establishment
        join ingest.catalog_candidates candidate
          on candidate.id = establishment.catalog_candidate_id
        """
    ).fetchone()

    readable = frozenset(security["readable_columns"] or ())
    expected = int(coverage["expected_publications"] or 0)
    eligible = int(coverage["eligible_publications"] or 0)
    checks = {
        "source_records_rls_enabled": bool(security["rls_enabled"]),
        "runtime_policy_enabled": bool(security["runtime_policy_enabled"]),
        "runtime_columns_least_privilege": readable == LIVE_DETAILS_SOURCE_COLUMNS,
        "all_expected_publications_eligible": expected > 0 and eligible == expected,
    }
    return {
        "healthy": all(checks.values()),
        "checks": checks,
        "expected_publications": expected,
        "eligible_publications": eligible,
        "publications_needing_live_hours_or_price": int(
            coverage["publications_needing_live_hours_or_price"] or 0
        ),
        "runtime_readable_source_columns": sorted(readable),
    }
