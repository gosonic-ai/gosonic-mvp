# Operational Test Data Reset Procedure

## Purpose

This document defines the controlled procedure for clearing Gosonic operational test data from the production database while preserving platform configuration.

This procedure is intended for early-stage platform validation only. It should be used when historical development/test calls are polluting operational views and a clean baseline is required for fresh test calls.

## Principle

Only operational records may be cleared.

Platform configuration, client records, settings, users, billing data, and integration configuration must be preserved.

## Tables Cleared

The following operational tables may be cleared during this procedure:

```sql
call_events
operational_events
workflow_instances
calls