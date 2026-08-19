alter table ingest.source_records
  drop constraint source_records_source_family_check,
  add constraint source_records_source_family_check check (
    source_family = any (array[
      'unknown',
      'government_regulator',
      'government_registry',
      'consumer_poi',
      'first_party',
      'community',
      'knowledge_graph'
    ]::text[])
  );
