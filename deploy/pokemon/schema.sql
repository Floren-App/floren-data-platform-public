-- Infrastructure-owned bootstrap. Programs cannot create/alter raw tables.
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS analytics;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE TABLE IF NOT EXISTS raw.card_observations (
 batch_id varchar NOT NULL,entity_id varchar NOT NULL,set_id varchar NOT NULL,payload varchar NOT NULL,
 content_hash varchar NOT NULL,observed_at timestamp(6) NOT NULL,
 PRIMARY KEY(batch_id,entity_id));
CREATE TABLE IF NOT EXISTS raw.set_observations (
 batch_id varchar NOT NULL,entity_id varchar NOT NULL,set_id varchar NOT NULL,payload varchar NOT NULL,
 content_hash varchar NOT NULL,observed_at timestamp(6) NOT NULL,
 PRIMARY KEY(batch_id,entity_id));
CREATE TABLE IF NOT EXISTS raw.ingestion_batches (
 batch_id varchar PRIMARY KEY,observed_at timestamp(6) NOT NULL,card_count integer NOT NULL,set_count integer NOT NULL);
