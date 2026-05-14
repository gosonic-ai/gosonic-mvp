# Gosonic Migration Policy

## Purpose

This directory governs all database schema evolution for the Gosonic platform.

All schema changes must be:
- versioned
- sequential
- reproducible
- reviewable
- recoverable

No direct production schema edits should occur outside tracked migrations.

---

## Migration Naming Convention

Format:

001_description.sql
002_description.sql
003_description.sql

Examples:

001_initial_platform_schema.sql
002_calls_operational_fields.sql
003_workflow_instances.sql

---

## Rules

1. Never modify old migrations after production deployment
2. Every schema change requires a new migration
3. Migrations must be additive whenever possible
4. Destructive changes require backups first
5. Production schema must always match migration history
6. Test migrations locally before deployment
7. Keep migrations small and isolated
8. Operational event persistence must remain backward-compatible

---

## Future Architecture Direction

The Gosonic platform database architecture is evolving toward:

- workflow orchestration
- immutable operational events
- lifecycle state management
- auditability
- multi-tenant operational infrastructure
- billing and usage accounting
- observability and telemetry systems

Migration discipline is foundational to platform reliability.