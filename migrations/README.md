# Gosonic Database Migrations

This directory contains versioned SQL migration files for the Gosonic platform database.

## Current Migration Baseline

- `001_initial_platform_schema.sql`

## Policy

All database schema changes must be introduced through sequential migration files.

Do not edit previously deployed migrations. Add a new migration instead.

## Naming Format

```plaintext
001_initial_platform_schema.sql
002_calls_operational_fields.sql
003_workflow_instances.sql
004_operational_events.sql