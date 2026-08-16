\set ON_ERROR_STOP on

CREATE DATABASE deskpilot_test;

\connect deskpilot_dev
CREATE EXTENSION IF NOT EXISTS vector;

\connect deskpilot_test
CREATE EXTENSION IF NOT EXISTS vector;
