# Gosonic Service Lifecycle Canon v2
## Authoritative Service Progression Standards — May 2026

---

# Purpose

This document defines the authoritative service lifecycle architecture governing operational workflow progression within the Gosonic platform.

This canon exists to ensure:
- deterministic workflow behavior
- backend-authoritative orchestration
- immutable operational history
- canonical transition enforcement
- operational auditability
- lifecycle consistency
- reusable orchestration semantics across Lite, Pro, Pro+, and Enterprise capabilities

The service lifecycle is one operational dimension within the broader Gosonic workflow system.

It is intentionally separate from:
- telephony lifecycle
- caller identity lifecycle
- notification lifecycle
- acknowledgement lifecycle
- booking lifecycle
- billing lifecycle
- escalation lifecycle

A workflow may progress operationally even while other lifecycle dimensions evolve independently.

---

# Foundational Principle

Service progression represents operational work progression after intake and triage have occurred.

Service progression is not:
- telephony completion
- notification completion
- workflow completion
- acknowledgement completion

A workflow may:
- have completed telephony
- have completed notifications
- still remain operationally active

The lifecycle canon exists to preserve these distinctions explicitly.

---

# Core Lifecycle Principles

1. Service progression is backend-authoritative.
2. Frontend surfaces render backend-authorized actions only.
3. Invalid lifecycle transitions must be rejected.
4. Terminal workflows cannot be reopened through standard progression.
5. Every lifecycle transition must persist immutable operational events.
6. Workflow state and service state are related but distinct.
7. Queue semantics derive from workflow/service state but are not authoritative truth.
8. Lifecycle semantics must remain reusable across verticals and plans.
9. Lifecycle progression must remain operationally observable.
10. Transition validation belongs to the backend orchestration layer.

---

# Lifecycle Dimensions

## Workflow State

Represents the overall orchestration condition of the workflow.

Examples:

```text
created
active
awaiting_external
paused
escalated
resolved
failed
cancelled
expired
```

Workflow state represents the overall operational condition of the workflow.

---

## Service State

Represents operational service progression.

Current Lite service states:

```text
triaged
awaiting_dispatch
scheduled
assigned
in_progress
resolved
failed
```

Service state represents operational execution progression.

---

## Notification State

Represents communication execution progression.

Examples:

```text
pending
business_sent
caller_sent
business_and_caller_sent
failed
```

Notification success does not imply service completion.

---

## Acknowledgement State

Represents whether operational receipt acknowledgement has occurred.

Examples:

```text
unacknowledged
acknowledged
```

Acknowledgement and service progression remain separate lifecycle dimensions.

---

## Caller Identity State

Represents caller identity completeness and operational trust state.

Examples:

```text
anonymous
partial
known
restricted
blocked
```

Caller identity state must remain separate from service progression.

---

# Current Lite Canonical Service Lifecycle

Current validated Lite progression:

```text
triaged
→ awaiting_dispatch
→ scheduled
→ assigned
→ in_progress
→ resolved
```

This lifecycle has been validated through:
- workflow persistence
- operational event persistence
- transition validation
- operator acknowledgement
- workflow resolution
- admin console rendering
- backend state enforcement

---

# Service State Definitions

## triaged

The intake has been analyzed and categorized.

The workflow is operationally active but has not entered dispatch progression.

Typical conditions:
- intake completed
- triage completed
- notifications may already be sent
- operator acknowledgement may or may not exist

---

## awaiting_dispatch

The workflow is operationally ready for dispatch or scheduling handling.

The workflow remains active.

This state does not imply assignment or scheduling completion.

---

## scheduled

A service appointment or operational commitment has been scheduled.

Scheduling does not necessarily imply assignment.

---

## assigned

A technician or operational resource has been assigned.

The workflow remains operationally active.

---

## in_progress

Operational work is actively occurring.

The workflow remains active until operational completion is confirmed.

---

## resolved

Operational work is considered completed.

The workflow transitions into terminal resolution semantics.

---

## failed

The workflow could not complete operationally.

Failure remains distinct from escalation.

Escalation may still represent an active workflow requiring intervention.

---

# Terminal States

The following workflow/service states are considered terminal:

```text
resolved
failed
cancelled
expired
```

Terminal workflows:
- expose no forward lifecycle actions
- cannot be reopened through standard progression
- remain immutable operational history
- preserve full auditability

Terminal workflows may still remain visible operationally for:
- reporting
- analytics
- auditing
- customer history
- reputation systems
- SLA review

---

# Transition Enforcement

Lifecycle transitions must be enforced exclusively by the backend orchestration layer.

Frontend applications must not:
- invent workflow transitions
- determine valid progression
- bypass transition rules
- reopen terminal workflows
- infer lifecycle truth independently

Transition validation currently includes rejection of:
- skipped transitions
- backward transitions
- reopening terminal workflows
- unauthorized transitions
- invalid terminal transitions
- policy-restricted transitions

The backend must reject invalid transitions explicitly.

---

# Current Allowed Lite Transitions

Current canonical Lite transitions:

```text
triaged → awaiting_dispatch
awaiting_dispatch → scheduled
scheduled → assigned
assigned → in_progress
in_progress → resolved
```

Current failure transition direction:

```text
active non-terminal state → failed
```

---

# Invalid Transition Examples

Rejected examples include:

```text
triaged → scheduled
scheduled → in_progress
resolved → scheduled
assigned → awaiting_dispatch
failed → assigned
resolved → in_progress
```

These transitions must return backend rejection responses.

---

# Workflow Resolution Philosophy

Workflow resolution is a terminal orchestration event.

Resolution means:
- operational work is considered complete
- no forward progression remains
- the workflow becomes historical operational record

Resolution does not necessarily imply:
- telephony completed successfully
- notifications succeeded
- customer satisfaction confirmed
- billing completed
- CRM synchronization completed

Resolution is operational lifecycle completion.

---

# Queue Semantics

Queue semantics are operational abstractions derived from backend workflow truth.

Queue semantics improve:
- operational scanning
- work organization
- operator cognition
- dashboard prioritization

Current examples:

```text
active + triaged → awaiting_service
active + awaiting_dispatch → awaiting_service
active + scheduled → active
resolved + resolved → resolved
failed + failed → failed
```

Queue semantics are not authoritative workflow truth.

The backend workflow/service state remains authoritative.

---

# Priority, Urgency, and Escalation

Urgency is not workflow state.

Priority is not escalation.

Escalation is not simply urgency.

Canonical separation:

```text
Urgency = severity of request
Workflow State = operational orchestration condition
Service State = operational progression
Queue State = operational grouping abstraction
Escalation = intervention or exceptional handling condition
```

An urgent workflow may still remain:

```text
Workflow: Active
Service: Awaiting Dispatch
Queue: Awaiting Service
Priority: Urgent
```

Escalated should remain reserved for exceptional operational intervention states.

---

# Event Persistence

Every lifecycle transition must persist immutable operational events.

Canonical lifecycle events include:

```text
service.triaged
service.awaiting_dispatch
service.scheduled
service.assigned
service.in_progress
service.resolved
service.failed

workflow.resolved
workflow.failed
workflow.cancelled
```

Operational history must remain:
- append-only
- immutable
- auditable
- observable
- timeline-renderable

Events must not be overwritten.

---

# Snapshot State Philosophy

Workflow instances may expose snapshot fields for efficient rendering.

Current snapshot fields include:

```text
workflow_status
current_stage
last_event_type
last_event_at
notification_state
service_state
queue_state
```

Snapshot fields summarize current truth.

Operational events preserve historical truth.

Both are required.

Snapshot fields must only be updated through backend-authoritative lifecycle helpers.

---

# Operator Governance Separation

Lifecycle progression and governance policy are separate concerns.

Lifecycle defines:
- what states exist
- what transitions are valid
- what events persist
- what states are terminal

Governance defines:
- who may perform transitions
- whether acknowledgement is required
- whether escalation blocks progression
- whether SLA conditions apply
- whether policy restrictions apply
- whether a plan tier restricts progression

Governance policy must not be embedded directly into frontend rendering logic.

---

# Notification Separation

Notification lifecycle and service lifecycle remain separate.

Examples:

A workflow may:
- successfully notify the business
- successfully notify the caller
- still remain awaiting dispatch

A workflow may:
- fail notifications
- still remain operationally active

Notification success does not equal workflow completion.

---

# Caller Identity Separation

Caller identity and service progression remain separate dimensions.

Examples:

A caller may be:
- anonymous
- partial
- known
- restricted

while the workflow simultaneously remains:
- active
- awaiting dispatch
- resolved
- failed

Caller identity state must never become shorthand for workflow completion.

---

# Current Hardening Direction

Current lifecycle hardening direction includes:

```text
notification gating behind durable persistence
stronger workflow prerequisite enforcement
operator authorization policy
future SLA governance
future dispatch orchestration
future booking orchestration
future customer identity enrichment
future ANI reputation handling
```

Lifecycle semantics must remain stable while orchestration sophistication increases.

---

# Future Lifecycle Expansion

Future lifecycle expansion may include:

```text
dispatch_pending
technician_en_route
awaiting_parts
customer_confirmation_pending
follow_up_required
recurring_service
quality_assurance_review
warranty_review
post_resolution_review
```

Future expansion must preserve:
- deterministic transitions
- backend authority
- immutable operational history
- lifecycle observability
- canonical operational semantics

---

# Current Architectural Direction

The backend owns:
- lifecycle truth
- transition validation
- action eligibility
- snapshot updates
- immutable event persistence
- queue derivation
- orchestration policy

The frontend renders:
- backend-authorized actions
- workflow visibility
- operational progression
- lifecycle diagnostics

This separation is foundational to Gosonic orchestration architecture.

---

# Current Validated Lifecycle Status

As of the May 18 checkpoint, Gosonic has validated:

```text
service-state transition validation
manual service progression
workflow resolution
immutable lifecycle events
workflow snapshot updates
queue-state derivation
priority/workflow separation
operator acknowledgement
backend-authoritative progression
terminal workflow enforcement
workflow-first operational rendering
```

This validates Lite as a functional operational lifecycle foundation.

---

# Canonical Lifecycle Rule

```text
Operational lifecycle truth must remain backend-authoritative.

Frontend systems may render lifecycle state.

They must not invent lifecycle truth.
```

---

# Canonical Statement

The Gosonic service lifecycle represents deterministic operational progression after intake has occurred.

The lifecycle exists to:
- coordinate operational work
- preserve operational truth
- enforce valid progression
- maintain auditability
- expose orchestration visibility
- support scalable workflow automation

The lifecycle architecture must scale from simple operational intake to enterprise-grade orchestration without changing its foundational semantics.

That is the service lifecycle canon.
