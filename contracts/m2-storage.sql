-- Proposition de schéma, pas une migration automatiquement exécutée.
-- REQ-ENG-002/004 ; POC-F2 ; propriétaire exclusif : retrieval.
BEGIN;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA atlas_retrieval;
CREATE TABLE atlas_retrieval.documents (
    doc_id text PRIMARY KEY CHECK (length(doc_id) > 0),
    source text NOT NULL UNIQUE CHECK (length(source) > 0),
    langue text NOT NULL CHECK (length(langue) > 0),
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[a-f0-9]{64}$'),
    embedding_revision text NOT NULL CHECK (length(embedding_revision) > 0),
    embedding_dimension integer NOT NULL CHECK (embedding_dimension BETWEEN 1 AND 1024)
);
CREATE TABLE atlas_retrieval.chunks (
    chunk_id text PRIMARY KEY CHECK (chunk_id ~ '^[a-f0-9]{64}$'),
    doc_id text NOT NULL REFERENCES atlas_retrieval.documents ON DELETE CASCADE,
    position integer NOT NULL CHECK (position >= 0),
    text text NOT NULL CHECK (length(btrim(text)) BETWEEN 1 AND 32000),
    embedding vector NOT NULL,
    CHECK (vector_dims(embedding) BETWEEN 1 AND 1024),
    UNIQUE (doc_id, position)
);
COMMIT;

-- Retour arrière proposé : transaction DROP SCHEMA atlas_retrieval CASCADE.
-- Ne pas supprimer l'extension partagée. Ce retour arrière efface l'index dérivé ;
-- le corpus source demeure intact et permet une réingestion.
-- Le service vérifie dimension/révision identiques pour tout l'index, valeurs
-- finies, vecteurs non nuls et cardinalité avant d'ouvrir la transaction d'écriture.
