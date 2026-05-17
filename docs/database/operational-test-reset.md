# Operational Test Data Reset Procedure

## Purpose

This document defines the controlled procedure for clearing Gosonic operational test data from the production database while preserving platform configuration.

This procedure is intended for early-stage platform validation only. It should be used when historical development/test calls are polluting operational views and a clean baseline is required for fresh test calls.

## Principle

Only operational records may be cleared.

Platform configuration, client records, settings, users, billing data, integration configuration, and account structure must be preserved.

This procedure must never be performed casually. It is destructive to operational test records and must always be preceded by a fresh database export.

## Tables Cleared

The following operational tables may be cleared during this procedure:

```sql
call_events
operational_events
workflow_instances
calls
```

These tables contain call records, workflow records, and event timelines generated during testing.

## Tables Not Cleared

The following tables must not be cleared as part of this procedure:

```sql
clients
client_settings
users
client_plans
invoices
invoice_line_items
client_contacts
client_addresses
```

These tables contain platform configuration, client setup, account data, billing/account structure, and integration-related configuration.

## Required Backup

Before running any reset command, create a fresh Render PostgreSQL export.

Recommended local backup location:

```text
C:\Users\David Davidian\Desktop\GOSONIC_AI\gosonic-platform\database\backups
```

Recommended filename format:

```text
gosonic_platform_pre_operational_reset_YYYY-MM-DD.dir.tar.gz
```

Example:

```text
gosonic_platform_pre_operational_reset_2026-05-17.dir.tar.gz
```

Do not proceed unless the backup has been downloaded and verified locally.

## Backup Verification

After downloading the export, verify it exists locally:

```powershell
Get-ChildItem "C:\Users\David Davidian\Desktop\GOSONIC_AI\gosonic-platform\database\backups" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 5 Name, LastWriteTime, Length
```

Confirm the new backup file appears in the list before continuing.

## Database Connection

Use the Render External Database URL locally in PowerShell.

Do not paste the database URL into documentation, Git, chat, screenshots, or logs.

```powershell
$dbUrl = "PASTE_RENDER_EXTERNAL_DATABASE_URL_HERE"
```

Verify `psql` is available:

```powershell
psql --version
```

Confirm database access with a read-only count:

```powershell
psql "$dbUrl" -c "SELECT COUNT(*) AS calls_count FROM calls;"
```

## Pre-Reset Verification

Before running the reset, verify current operational record counts:

```powershell
psql "$dbUrl" -c "
SELECT 'calls' AS table_name, COUNT(*) FROM calls
UNION ALL
SELECT 'call_events', COUNT(*) FROM call_events
UNION ALL
SELECT 'workflow_instances', COUNT(*) FROM workflow_instances
UNION ALL
SELECT 'operational_events', COUNT(*) FROM operational_events
ORDER BY table_name;
"
```

Review the counts before proceeding.

## Reset Command

Run the reset only after backup and count verification are complete.

```powershell
psql "$dbUrl" -c "
BEGIN;

TRUNCATE TABLE
  call_events,
  operational_events,
  workflow_instances,
  calls
RESTART IDENTITY;

COMMIT;
"
```

Expected output:

```text
BEGIN
TRUNCATE TABLE
COMMIT
```

## Post-Reset Verification

Immediately verify the operational tables are empty:

```powershell
psql "$dbUrl" -c "
SELECT 'calls' AS table_name, COUNT(*) FROM calls
UNION ALL
SELECT 'call_events', COUNT(*) FROM call_events
UNION ALL
SELECT 'workflow_instances', COUNT(*) FROM workflow_instances
UNION ALL
SELECT 'operational_events', COUNT(*) FROM operational_events
ORDER BY table_name;
"
```

Expected result:

```text
call_events        0
calls              0
operational_events 0
workflow_instances 0
```

## API Health Verification

Confirm the backend health endpoint:

```powershell
Invoke-RestMethod `
  -Uri "https://gosonic-mvp.onrender.com/" `
  -Method GET
```

Expected result:

```text
status: ok
service: Gosonic MVP API
```

## Authenticated API Verification

Refresh the admin token if required.

Do not paste the admin key into documentation, Git, chat, screenshots, or logs.

```powershell
$adminKey = "PASTE_CURRENT_ADMIN_API_KEY_HERE"

$tokenResponse = Invoke-RestMethod `
  -Uri "https://gosonic-mvp.onrender.com/auth/admin-token" `
  -Method POST `
  -Headers @{
    "x-admin-key" = $adminKey
  }

$token = $tokenResponse.token
$token.Length
```

Confirm the token length is non-zero.

Then confirm the authenticated `/calls` endpoint returns zero records:

```powershell
$callsResponse = Invoke-RestMethod `
  -Uri "https://gosonic-mvp.onrender.com/calls" `
  -Method GET `
  -Headers @{
    Authorization = "Bearer $token"
  }

$callsResponse.status
$callsResponse.count
```

Expected result:

```text
ok
0
```

## Admin Console Verification

Refresh the Gosonic client admin console and verify the Calls view is empty.

Expected result:

```text
Call activity will appear here.
```

or no visible call rows.

## Required Fresh Baseline Tests

After reset, perform fresh live test calls to rebuild a clean operational baseline.

Minimum recommended test set:

1. Standard confirmed request
2. Urgent confirmed request

For each call, validate:

```text
Call persistence
Workflow creation
Operational timeline
Workflow timeline
Business SMS
Caller SMS
/calls snapshot fields
Admin console row rendering
Expanded call detail rendering
```

## Expected Baseline States

A standard confirmed request should display:

```text
Workflow: Awaiting Service
Priority: Standard
Intake: Confirmed
Notify: B + C
```

An urgent confirmed request should display:

```text
Workflow: Awaiting Service
Priority: Urgent
Intake: Confirmed
Notify: B + C
```

Urgency/priority must not automatically force workflow escalation.

## Escalation Semantics

Priority and workflow state are separate concepts.

Priority describes request severity:

```text
standard
urgent
emergency
```

Workflow describes operational handling state:

```text
active
awaiting_service
escalated
resolved
failed
```

An urgent request should only display as escalated if the workflow itself enters a true escalation/intervention state.

Examples of true escalation may include:

```text
workflow_status = escalated
notification failure requiring intervention
after-hours emergency requiring manual escalation
operator intervention required
SLA-sensitive workflow not acknowledged
dispatch unavailable
manual escalation triggered
```

## Post-Test API Verification

After fresh test calls, verify `/calls` state:

```powershell
$callsResponse = Invoke-RestMethod `
  -Uri "https://gosonic-mvp.onrender.com/calls" `
  -Method GET `
  -Headers @{
    Authorization = "Bearer $token"
  }

$callsResponse.calls |
  Select-Object `
    created_at,
    caller_name,
    urgency,
    workflow_status,
    queue_state,
    service_state,
    notification_state |
  Format-Table -AutoSize
```

Expected example:

```text
caller_name      urgency   workflow_status   queue_state        service_state   notification_state
-----------      -------   ---------------   -----------        -------------   ------------------
David Davidian   standard  active            awaiting_service   triaged         caller_sent
Adam Shaw        urgent    active            awaiting_service   triaged         caller_sent
```

## Procedure Completion Checklist

Before considering the reset complete, confirm:

```text
Fresh Render PostgreSQL export created
Export downloaded locally
Export filename is clear and date-stamped
Pre-reset operational table counts reviewed
Reset SQL executed successfully
Post-reset operational table counts are zero
Backend health endpoint returns ok
Authenticated /calls endpoint returns zero
Admin console shows no calls
Fresh standard test call validated
Fresh urgent test call validated
Priority/workflow separation confirmed
Backend and frontend repos remain clean or committed
```

## Notes

This reset procedure is destructive to operational test records.

Do not run it casually.

Do not run it without a fresh database export.

Do not use it to clear platform configuration data.

This procedure should eventually be replaced or supplemented by a dedicated staging environment and seeded test-data tools.