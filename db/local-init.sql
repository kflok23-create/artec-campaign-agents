-- Runs once when the local compose Postgres is first created.
-- The test suite uses its own database so it can truncate tables freely.
CREATE DATABASE artec_agents_test;
