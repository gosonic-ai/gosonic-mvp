# Gosonic Lite Service Plan Canon

## Purpose

The Lite service plan defines the first production-capable Gosonic workflow tier.

Lite is not a billing construct first. It is an operational capability profile that describes what the platform can reliably perform for a client using the current voice intake, workflow, notification, and manual service progression infrastructure.

This document defines the canonical capabilities, boundaries, workflow expectations, and future upgrade path for Lite.

## Plan Name

```text
Lite Voice Intake
```

## Plan Tier

```text
lite
```

## Current Platform Mapping

The Lite plan maps to the existing client-level field:

```text
clients.plan_tier = 'lite'
```

The current backend also supports a `client_plans` table for future billing/account structure. At this stage, `clients.plan_tier` remains the authoritative lightweight plan indicator.

## Core Capability

Lite provides structured voice intake and operational notification for service businesses.

The canonical Lite workflow is:

```text
Inbound call
→ voice intake
→ caller/contact capture
→ service request capture
→ priority classification
→ confirmation
→ call persistence
→ workflow creation
→ business notification
→ caller confirmation
→ service triage
→ manual service progression
→ workflow resolution
```

## Included Capabilities

Lite includes the following capabilities.

### Voice Intake

```text
Inbound voice call handling
Caller name capture
Caller phone capture
Service address capture
Issue description capture
Basic preferred-time capture when available
Call outcome confirmation
Transcript capture
```

### Classification

```text
Priority classification
Issue-type classification
Standard vs urgent handling
Model confidence capture
Escalation reason capture when applicable
```

Priority is separate from workflow state.

```text
Priority = request severity
Workflow = operational handling state
```

An urgent request does not automatically mean the workflow is escalated.

### Notifications

```text
Business SMS notification
Caller SMS confirmation
Notification state tracking
SMS policy reason capture
Business notification success/failure capture
Caller notification success/failure capture
```

### Workflow Infrastructure

```text
Workflow creation
Workflow status tracking
Current stage tracking
Service state tracking
Notification state tracking
Last event tracking
Immutable workflow event timeline
Operational call event timeline
```

### Service Lifecycle

Lite supports manual service progression through the canonical service states:

```text
triaged
awaiting_dispatch
scheduled
assigned
in_progress
resolved
failed
```

At the Lite level, these states may be advanced manually by platform/admin operations rather than through client self-service or automated dispatch integrations.

### Admin Visibility

Lite includes operational visibility in the admin console:

```text
Calls queue
Workflow queue state
Priority
Intake outcome
Notification state
Expanded call detail
Lifecycle summary
Workflow diagnostics
Operational timeline
Workflow timeline
Recent activity
Queue metrics
```

## Canonical Lite Queue Behavior

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

Escalated should be reserved for true workflow intervention states, not urgency alone.

## Excluded From Lite

Lite does not include the following as first-class automated capabilities:

```text
Client self-service workflow controls
Automated technician dispatch
Calendar booking integration
CRM integration
Address validation API
Inbound ANI customer matching
Operator acknowledgement SLA automation
Multi-user role-based dispatch console
Automated invoice generation
Advanced service territory routing
```

These belong to future Pro, Pro+, or enterprise capability layers.

## Upgrade Path

Lite forms the base operational layer for future plans.

Future plan evolution may include:

```text
Pro:
Calendar and booking integrations
Operator acknowledgement
Client-facing workflow controls
Structured availability logic

Pro+:
Dispatch lifecycle
Technician assignment
Territory/routing intelligence
Service completion tracking
Escalation/SLA automation

Enterprise:
Custom orchestration policies
Multi-location operations
CRM/ERP integration
Advanced analytics
Custom workflow definitions
```

## Engineering Principles

Lite must remain:

```text
stable
auditable
event-driven
workflow-first
configuration-aware
safe for client operations
```

All Lite functionality must preserve the Gosonic architecture principles:

```text
backend owns truth
events are immutable
workflow state is authoritative
priority is separate from workflow escalation
notifications are tracked explicitly
UI reflects backend state rather than inventing state
```

## Current Known Retell Refinement Notes

Current Retell/agent refinement items that may affect Lite quality later:

```text
Maintenance/service-call intent currently maps too readily to no_heat.
The agent once exposed JSON aloud: {"call_outcome":"confirmed"}.
Address capture sometimes requires correction/retry.
```

These are Retell prompt/extraction refinements, not failures of the Lite workflow infrastructure.

## Current Validation Status

As of the May 17 validation checkpoint, Lite has been validated with:

```text
Clean operational database baseline
Fresh standard confirmed request
Fresh urgent confirmed request
Business SMS delivery
Caller SMS delivery
Workflow creation
Operational timeline
Workflow timeline
Admin console rendering
Priority/workflow separation
Manual service-state advancement
Workflow event persistence checks
```

## Implementation Status

```text
Status: Foundational live validation complete
Plan tier field: clients.plan_tier = 'lite'
Billing enforcement: not implemented
Self-service plan switching: not implemented
Client-facing plan UI: not implemented
```

## Notes

Lite should be treated as the first stable Gosonic operational service profile.

It is intentionally narrow.

It should become excellent before larger plans are built.