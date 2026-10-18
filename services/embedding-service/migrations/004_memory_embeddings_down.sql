-- 004_memory_embeddings_down.sql — DOWN for corridor-store 004_memory_embeddings.sql
--
-- Deliberately NOT placed in corridor-store/migrations/: the models-owned
-- migrate runner applies every corridor-store/migrations/*.sql lexically as
-- an UP, so the down script lives here (embedding-service) where the
-- embedding stream owns the path.
--
-- Reverses 004 exactly: drops the three new tables (indexes go with them).
-- Additive-only up means nothing pre-existing needs restoring.

DROP TABLE IF EXISTS traffic.memory_segment_embeddings;
DROP TABLE IF EXISTS traffic.incidents;
DROP TABLE IF EXISTS traffic.memory_docs;
