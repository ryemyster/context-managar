-- Code embeddings table for context-engine vector search
-- Run this in your Supabase SQL editor or via supabase db push
--
-- Requires: pgvector extension
-- Embedding model: nomic-embed-text (768 dimensions)

-- Enable pgvector
create extension if not exists vector;

-- Drop and recreate if schema changes
-- (comment this out if you want to preserve existing data)
-- drop table if exists code_embeddings;

create table if not exists code_embeddings (
  id          bigserial primary key,
  path        text        not null,
  chunk       text        not null,
  chunk_hash  text        not null,
  embedding   vector(768),
  indexed_at  timestamptz default now(),

  constraint code_embeddings_hash_unique unique (chunk_hash)
);

-- Index for fast similarity search
-- ivfflat is appropriate for CPU Supabase (no GPU)
-- lists = 50 is good for up to ~50k rows
create index if not exists code_embeddings_embedding_idx
  on code_embeddings
  using ivfflat (embedding vector_cosine_ops)
  with (lists = 50);

-- Index for path lookups
create index if not exists code_embeddings_path_idx
  on code_embeddings (path);

-- Match function used by context-engine /vector-search and /context
create or replace function match_code_embeddings(
  query_embedding vector(768),
  match_count     int     default 8,
  match_threshold float   default 0.6
)
returns table (
  id          bigint,
  path        text,
  chunk       text,
  similarity  float
)
language sql stable
as $$
  select
    id,
    path,
    chunk,
    1 - (embedding <=> query_embedding) as similarity
  from code_embeddings
  where 1 - (embedding <=> query_embedding) > match_threshold
  order by embedding <=> query_embedding
  limit match_count;
$$;

-- Useful queries for maintenance
-- SELECT path, count(*) FROM code_embeddings GROUP BY path ORDER BY count DESC;
-- DELETE FROM code_embeddings WHERE path = 'src/old-file.ts';
-- SELECT count(*) FROM code_embeddings;
