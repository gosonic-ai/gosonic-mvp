# Gosonic Service Lifecycle Canon
## Authoritative Service Progression Standards
### Canon Version: v3
### Authoritative Revision: May 2026

---

## 1. Purpose

This document defines the authoritative service lifecycle architecture governing operational workflow progression inside the Gosonic platform.

It exists to ensure deterministic workflow behavior, backend-authoritative orchestration, immutable operational history, canonical transition enforcement, operational auditability, lifecycle consistency, service-stage synchronization, and reusable orchestration semantics.

The service lifecycle is one operational dimension within the broader Gosonic workflow system.

It is intentionally separate from telephony lifecycle, caller identity lifecycle, notification lifecycle, ownership lifecycle, booking lifecycle, billing lifecycle, and escalation lifecycle.

A workflow may progress operationally while other lifecycle dimensions evolve independently.

---

## 2. Foundational Principle

Service progression represents operational work progression after intake and triage have occurred.

Service progression is not telephony completion, notification completion, ownership acknowledgement, workflow completion, billing completion, or customer satisfaction confirmation.

A workflow may have completed telephony and notifications while remaining operationally active.

The service lifecycle exists to preserve these distinctions explicitly.

---

## 3. Core Lifecycle Rules

1. Service progression is backend-authoritative.
2. Frontend surfaces render backend-authorized actions only.
3. Invalid lifecycle transitions must be rejected.
4. Terminal workflows cannot be reopened through standard progression.
5. Every lifecycle transition must persist immutable operational events.
6. Workflow status and service state are related but distinct.
7. Service state, current stage, and event stage must remain synchronized during canonical service progression.
8. Queue semantics derive from backend truth but are not authoritative truth.
9. Lifecycle progression must remain observable in workflow diagnostics and event timelines.
10. Transition validation belongs to the backend orchestration layer.

---

## 4. Lifecycle Dimensions

### Workflow Status

Represents the overall operational condition of the workflow.

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

### Current Stage

Represents the current operational phase within the workflow lifecycle.

For service progression, this must synchronize with `service_state`.

### Service State

Represents the operational service execution position.

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

### Notification State

Represents communication execution.

Examples:

```text
business_sent
caller_sent
business_and_caller_sent
failed
```

Notification success does not imply service completion.

### Ownership State

Represents operational responsibility.

Examples:

```text
acknowledged
assigned
transferred
released
```

Ownership is separate from service state.

---

## 5. Current Lite Canonical Service Lifecycle

Current validated Lite progression:

```text
triaged
→ awaiting_dispatch
→ scheduled
→ assigned
→ in_progress
→ resolved
```

This lifecycle has been validated through workflow persistence, operational event persistence, transition validation, ownership acknowledgement, ownership assignment, workflow resolution, admin console rendering, backend state enforcement, and stage synchronization.

---

## 6. Service State Definitions

### triaged

The intake has been analyzed and categorized. The workflow is operationally active but has not entered service execution progression.

Typical conditions include intake completed, triage completed, service request classified, notifications may already be sent, and ownership acknowledgement may or may not exist.

### awaiting_dispatch

The workflow is operationally ready for dispatch or scheduling handling. This state does not imply scheduling, assignment, or work commencement.

### scheduled

A service appointment or operational commitment has been scheduled. Scheduling does not necessarily imply assignment.

### assigned

A technician, operator, or operational resource has been assigned.

In Lite, assignment currently creates an ownership assignment side-event and updates:

```text
ownership_state=assigned
assigned_operator
assigned_team
```

### in_progress

Operational work is actively occurring. The workflow remains active until operational completion is confirmed.

### resolved

Operational work is considered complete. This transitions into terminal workflow resolution semantics.

### failed

The service workflow could not complete operationally. Failure remains distinct from escalation.

---

## 7. Canonical Synchronization Rule

For canonical Lite service progression, these values must remain synchronized:

```text
service_state
current_stage
event_stage
```

Validated mapping:

```text
triaged            → triaged
awaiting_dispatch  → awaiting_dispatch
scheduled          → scheduled
assigned           → assigned
in_progress        → in_progress
resolved           → resolved
failed             → failed
```

Example:

```text
service.awaiting_dispatch
service_state=awaiting_dispatch
current_stage=awaiting_dispatch
event_stage=awaiting_dispatch
```

This rule prevents lifecycle drift.

The backend lifecycle helper responsible for service progression must update these together.

---

## 8. Workflow Status Relationship

`workflow_status` represents the overall workflow condition. `service_state` represents service progression.

During active service progression:

```text
workflow_status=active
service_state=awaiting_dispatch
```

or:

```text
workflow_status=active
service_state=scheduled
```

At terminal completion:

```text
workflow_status=resolved
service_state=resolved
current_stage=resolved
last_event_type=workflow.resolved
```

Workflow status should not be used as a substitute for service state. Service state should not be used as a substitute for workflow status.

---

## 9. Terminal States

The following workflow/service outcomes are terminal:

```text
resolved
failed
cancelled
expired
```

Terminal workflows expose no forward lifecycle actions, cannot be reopened through standard progression, remain immutable operational history, and preserve full auditability.

Terminal workflows may still remain visible for reporting, analytics, audit review, customer history, reputation systems, and SLA review.

---

## 10. Transition Enforcement

Lifecycle transitions must be enforced exclusively by the backend orchestration layer.

Frontend applications must not invent workflow transitions, determine valid progression, bypass transition rules, reopen terminal workflows, or infer lifecycle truth independently.

Transition validation must reject skipped transitions, backward transitions, reopening terminal workflows, unauthorized transitions, invalid terminal transitions, and policy-restricted transitions.

---

## 11. Current Allowed Lite Transitions

Current canonical Lite transitions:

```text
triaged → awaiting_dispatch
awaiting_dispatch → scheduled
scheduled → assigned
assigned → in_progress
in_progress → resolved
```

Failure may be allowed from active non-terminal service states:

```text
active non-terminal state → failed
```

---

## 12. Invalid Transition Examples

Rejected examples include:

```text
triaged → scheduled
scheduled → in_progress
resolved → scheduled
assigned → awaiting_dispatch
failed → assigned
resolved → in_progress
```

These transitions must return explicit backend rejection responses.

---

## 13. Service Events

Every lifecycle transition must persist immutable operational events.

Canonical service events include:

```text
service.triaged
service.awaiting_dispatch
service.scheduled
service.assigned
service.in_progress
service.resolved
service.failed
```

Resolution may produce `workflow.resolved` rather than a separate `service.resolved` event when the terminal operation resolves the workflow as a whole.

Operational history must remain append-only, immutable, auditable, observable, and timeline-renderable.

---

## 14. Ownership Side-Effects

Service assignment may produce ownership assignment.

Validated Lite behavior:

```text
service.assigned
→ ownership.assigned
→ ownership_state=assigned
→ assigned_operator=dashboard_operator
→ assigned_team=operations
```

Ownership side-events must remain explicit.

Service assignment and ownership assignment are related, but not identical.

The service event records service progression. The ownership event records responsibility assignment. Both are useful in the audit trail.

---

## 15. Workflow Resolution Philosophy

Workflow resolution is a terminal orchestration event.

Resolution means operational work is considered complete, no forward progression remains, and the workflow becomes a historical operational record.

Resolution does not necessarily imply customer satisfaction confirmed, billing completed, CRM synchronized, or post-service QA completed.

Resolution is operational lifecycle completion for the current service workflow.

---

## 16. Queue Semantics

Queue semantics are operational abstractions derived from backend workflow truth.

Queue semantics improve operational scanning, work organization, operator cognition, and dashboard prioritization.

Queue semantics are not authoritative workflow truth.

Examples:

```text
active + triaged           → awaiting_service
active + awaiting_dispatch → awaiting_service
active + scheduled         → awaiting_service or active
resolved + resolved        → resolved
failed + failed            → failed
```

Backend workflow and service state remain authoritative.

---

## 17. Priority, Urgency, and Escalation

Urgency is not workflow state. Priority is not escalation. Escalation is not simply urgency.

Canonical separation:

```text
Urgency       = severity of request
Workflow      = orchestration condition
Service State = operational progression
Queue State   = operational grouping abstraction
Escalation    = intervention-required condition
```

An urgent workflow may remain:

```text
Workflow: Active
Service: Awaiting Dispatch
Priority: Urgent
```

Escalated should remain reserved for exceptional operational intervention states.

---

## 18. Snapshot State Philosophy

Workflow instances expose snapshot fields for efficient rendering.

Current snapshot fields include:

```text
workflow_status
current_stage
last_event_type
last_event_at
notification_state
service_state
ownership_state
assigned_operator
assigned_team
queue_state
```

Snapshot fields summarize current truth. Operational events preserve historical truth. Both are required.

Snapshot fields must only be updated through backend-authoritative lifecycle helpers.

---

## 19. Operator Governance Separation

Lifecycle progression and governance policy are separate concerns.

Lifecycle defines what states exist, what transitions are valid, what events persist, what states are terminal, and what snapshot fields change.

Governance defines who may perform transitions, whether acknowledgement is required, whether escalation blocks progression, whether SLA conditions apply, whether policy restrictions apply, and whether a plan tier restricts progression.

Governance policy must not be embedded directly into frontend rendering logic.

---

## 20. Notification Separation

Notification lifecycle and service lifecycle remain separate.

A workflow may have business notified, caller notified, service awaiting dispatch, and workflow active.

A workflow may fail notifications and still remain operationally active.

Notification success does not equal workflow completion.

---

## 21. Caller Identity Separation

Caller identity and service progression remain separate dimensions.

A caller may be anonymous, partial, known, restricted, or blocked while the workflow simultaneously remains active, awaiting dispatch, resolved, or failed.

Caller identity state must never become shorthand for workflow completion.

---

## 22. Current Validated Lifecycle Status

As of the May 19 orchestration checkpoint, Gosonic has validated:

```text
service-state transition validation
manual service progression
workflow-stage synchronization
service_state/current_stage/event_stage synchronization
ownership acknowledgement
ownership assignment side-events
workflow resolution
immutable lifecycle events
workflow snapshot updates
queue-state derivation
priority/workflow separation
backend-authoritative progression
terminal workflow enforcement
workflow-first operational rendering
```

This validates Lite as a complete operational lifecycle foundation.

---

## 23. Current Hardening Direction

Current lifecycle hardening direction includes authenticated operator identity, role-aware operator authorization, notification gating behind durable persistence, SLA governance, aging/stale workflow detection, dispatch orchestration, booking orchestration, customer identity enrichment, ANI reputation handling, and address verification.

Lifecycle semantics must remain stable while orchestration sophistication increases.

---

## 24. Future Lifecycle Expansion

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

Future expansion must preserve deterministic transitions, backend authority, immutable operational history, lifecycle observability, canonical operational semantics, and stage synchronization discipline.

---

## 25. Canonical Rule

```text
Operational lifecycle truth must remain backend-authoritative.

Frontend systems may render lifecycle state.

They must not invent lifecycle truth.

Service state, current stage, and event stage must remain synchronized during canonical service progression.
```

That is the service lifecycle canon.
