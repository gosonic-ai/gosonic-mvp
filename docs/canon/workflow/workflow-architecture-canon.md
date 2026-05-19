# Gosonic Workflow Architecture Canon
## Authoritative Workflow & Orchestration Standards
### Canon Version: v3
### Authoritative Revision: May 2026

---

## 1. Purpose

This document defines the canonical workflow architecture of the Gosonic platform.

It exists to preserve architectural continuity across backend orchestration, workflow state management, operational event taxonomy, service lifecycle progression, operator governance, ownership semantics, Retell voice-agent behavior, client/admin operational interfaces, and future Pro, Pro+, and Enterprise capabilities.

This document is the source canon for how Gosonic understands, represents, advances, governs, and observes operational workflows.

Gosonic is not a call-answering layer. Gosonic is operational communication infrastructure.

A voice call may begin a workflow, but the workflow may continue after the call ends. The platform must understand what happened, what state the work is in, who or what owns the next operational step, whether the workflow is complete, and whether the operational trail can be audited later.

---

## 2. Foundational Architecture Principle

Gosonic must be built around:

```text
events
workflow state
lifecycle semantics
backend-authoritative truth
durable operational records
observable operational progression
```

It must not be built around screens, demos, or isolated call flows.

The UI displays operational truth. The backend owns operational truth. The database preserves operational truth. The voice agent initiates, enriches, and structures operational events, but the voice agent is not the whole system.

The workflow architecture must remain reusable across Lite, Pro, Pro+, and Enterprise capabilities.

---

## 3. Canonical Object Model

```text
Client
  └── Caller / ANI Context
        └── Call
              └── Workflow Instance
                    ├── Operational Events
                    ├── Workflow Snapshot
                    ├── Service State
                    ├── Notification State
                    ├── Ownership State
                    ├── Operator Actions
                    └── Timeline
```

A caller identity context may exist before a workflow is complete. A phone number may exist before a caller is fully identified. A call may exist before a workflow is actionable. A workflow may exist after the call has ended.

The call is one operational surface. The workflow is the broader operational container.

---

## 4. Workflow Instance

A `workflow_instance` represents the durable operational job created from a call or communication event.

It is not merely a call record. It is the container for the operational work that follows communication activity.

A validated Lite workflow may include:

```text
call analyzed
workflow created
intake completed
triage completed
service triaged
business notified
caller notified
ownership acknowledged
service awaiting dispatch
service scheduled
service assigned
ownership assigned
service in progress
workflow resolved
```

The call may be complete while the workflow remains active. That distinction is foundational.

---

## 5. Canonical Lifecycle Dimensions

Gosonic workflows are not represented by a single shallow status. Operational reality is represented through layered lifecycle dimensions.

Canonical dimensions include:

```text
telephony state
intake state
caller identity state
workflow status
current stage
notification state
ownership state
service state
booking state
dispatch state
resolution state
queue state
```

Not every plan uses every dimension.

Lite currently emphasizes telephony state, intake state, workflow status, current stage, notification state, ownership state, service state, resolution state, and queue state.

Pro adds booking semantics. Pro+ adds dispatch depth and advanced assignment semantics. Enterprise extends the same core with role governance, SLA policy, integrations, analytics, and custom workflow definitions.

No plan tier should require a separate workflow architecture.

---

## 6. Workflow Status

`workflow_status` represents the overall operational condition of the workflow.

Current canonical workflow statuses include:

```text
created
active
awaiting_external
escalated
paused
resolved
completed
cancelled
failed
expired
```

Lite primarily uses:

```text
created
active
resolved
failed
cancelled
```

Workflow status is separate from telephony status, current stage, service state, notification state, ownership state, urgency, queue state, and UI display labels.

Events are history. Snapshot state is current truth. Both are required.

---

## 7. Current Stage

`current_stage` represents the workflow’s current operational stage inside the orchestration lifecycle.

It is the stage-facing companion to service progression.

For canonical Lite service progression, the following must remain synchronized:

```text
service_state
current_stage
event_stage
```

Validated Lite synchronization:

```text
service_state=awaiting_dispatch → current_stage=awaiting_dispatch → event_stage=awaiting_dispatch
service_state=scheduled         → current_stage=scheduled         → event_stage=scheduled
service_state=assigned          → current_stage=assigned          → event_stage=assigned
service_state=in_progress       → current_stage=in_progress       → event_stage=in_progress
service_state=resolved          → current_stage=resolved          → event_stage=resolved
```

Ownership acknowledgement may temporarily set `current_stage=acknowledged`, but service progression must then advance `current_stage` through the service lifecycle.

This prevents stage drift and ensures the workflow snapshot reflects the latest operational progression.

---

## 8. Service State

`service_state` represents operational service progression after intake and triage.

Current validated Lite service states:

```text
triaged
awaiting_dispatch
scheduled
assigned
in_progress
resolved
failed
```

Current validated Lite progression:

```text
triaged
→ awaiting_dispatch
→ scheduled
→ assigned
→ in_progress
→ resolved
```

Service state is separate from workflow status.

A workflow may be `workflow_status=active` and `service_state=scheduled`.

A workflow becomes terminal when service completion or failure is resolved into workflow-level terminal status.

Service progression must be backend-authoritative. Invalid transitions must be rejected by the backend. Terminal workflows must not expose forward progression actions.

---

## 9. Notification State

`notification_state` represents communication execution.

Current notification states include:

```text
pending
business_sent
caller_sent
business_and_caller_sent
business_failed
caller_failed
failed
```

Notification success does not imply service completion.

A workflow may have business SMS sent, caller SMS sent, service awaiting dispatch, and workflow still active.

Notification events must be explicit, immutable, and auditable.

Canonical notification events include:

```text
notification.business_sent
notification.business_failed
notification.caller_sent
notification.caller_failed
```

---

## 10. Ownership State

`ownership_state` represents operational responsibility.

Ownership is separate from acknowledgement, service execution, and workflow completion.

Current validated ownership states include:

```text
acknowledged
assigned
released
transferred
```

Current validated ownership events:

```text
ownership.acknowledged
ownership.assigned
```

Ownership acknowledgement means the business/operator has confirmed receipt of the workflow. Ownership assignment means a responsible operator or team has been assigned.

Acknowledgement does not mean scheduled, assigned, in progress, or resolved. Assignment does not mean in progress or resolved.

Current validated ownership snapshot fields:

```text
ownership_state
assigned_operator
assigned_team
```

Validated Lite pattern:

```text
ownership.acknowledged → ownership_state=acknowledged
service.assigned      → ownership.assigned → ownership_state=assigned
```

Ownership is now a first-class orchestration dimension.

---

## 11. Caller Identity State

Caller identity is separate from workflow progression.

Current caller identity fields include:

```text
caller_phone
caller_phone_source
caller_identity_status
caller_phone_verified
```

Canonical caller phone sources include:

```text
ani
spoken
manual
crm
unknown
```

Canonical caller identity states include:

```text
anonymous
partial
known
restricted
blocked
```

Permanent architecture rule:

```text
phone known
≠ caller known
≠ request valid
≠ workflow complete
≠ operationally trusted
```

ANI is authoritative telephony metadata, but it is not sufficient by itself to establish customer trust or workflow validity.

Caller identity will later support repeat-caller recognition, CRM matching, address prefill, ANI reputation, restricted routing, VIP handling, and spam/nuisance controls.

---

## 12. Urgency, Priority, Escalation, and Queue State

Urgency is not workflow status. Priority is not escalation. Escalation is not simply urgency.

Canonical separation:

```text
Urgency / Priority = severity of request
Workflow Status    = operational condition of the workflow
Service State      = service progression
Queue State        = operational grouping abstraction
Escalation         = intervention-required operational condition
```

A confirmed urgent request may remain:

```text
workflow_status=active
service_state=awaiting_dispatch
queue_state=awaiting_service
urgency=urgent
```

Escalated should remain reserved for true operational intervention states such as SLA failure, blocked progression, operator exception, policy violation, or unresolved intervention requirement.

Queue state is derived for operational scanning. Queue state must not become authoritative workflow truth.

---

## 13. Operational Events

Every meaningful system action must produce an immutable operational event.

Events are append-only, historically meaningful, operationally observable, timeline-renderable, audit-compatible, and orchestration-compatible.

Current canonical event domains:

```text
call.*
workflow.*
intake.*
triage.*
notification.*
ownership.*
service.*
booking.*
dispatch.*
caller.*
```

Validated Lite event chain:

```text
workflow.created
intake.completed
triage.completed
service.triaged
notification.business_sent
notification.caller_sent
ownership.acknowledged
service.awaiting_dispatch
service.scheduled
service.assigned
ownership.assigned
service.in_progress
workflow.resolved
```

Events must not be overwritten. They become operational history. The UI timeline should be derived from events.

---

## 14. Workflow Snapshot State

A workflow instance may expose snapshot fields for efficient rendering and operational queueing.

Current snapshot-style fields include:

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

Snapshot fields must be updated only through backend-authoritative workflow helpers. Frontend surfaces must not invent workflow state independently.

---

## 15. Operator Actions

Operator actions are backend-authoritative.

The backend determines which actions are available, whether the workflow is terminal, whether acknowledgement exists, whether a transition is valid, whether ownership is affected, whether a role or plan tier may perform the action, what event is persisted, and what snapshot fields change.

The frontend renders allowed actions. The frontend does not define operational policy.

Current validated Lite operator progression:

```text
triaged
→ awaiting_dispatch
→ scheduled
→ assigned
→ in_progress
→ resolved
```

Terminal workflows expose no forward actions.

---

## 16. Canonical Lite Workflow

Validated Lite workflow:

```text
call analyzed
workflow created
intake completed
triage completed
service triaged
business notified
caller notified
ownership acknowledged
service awaiting dispatch
service scheduled
service assigned
ownership assigned
service in progress
workflow resolved
```

Validated terminal snapshot:

```text
workflow_status=resolved
current_stage=resolved
service_state=resolved
ownership_state=assigned
last_event_type=workflow.resolved
```

Lite is now a complete operational workflow foundation.

Lite must remain narrow, stable, auditable, and excellent before Pro and Pro+ expansion.

---

## 17. Plan Capability Layers

### Lite

Lite includes voice intake, triage, call summary, ANI capture, caller phone metadata, caller confirmation, business SMS notification, caller SMS confirmation, call persistence, workflow persistence, notification tracking, ownership acknowledgement, manual service progression, workflow timelines, operational call timelines, and workflow resolution.

### Pro

Pro adds calendar availability, booking requests, booking confirmation, structured availability logic, CRM/contact synchronization, customer confirmation, customer identity enrichment, address validation/prefill, workflow booking state, and client-facing workflow controls.

### Pro+

Pro+ adds dispatch lifecycle, operator acknowledgement policy depth, technician assignment depth, dispatch status, service completion tracking, territory/routing intelligence, advanced escalation handling, SLA automation, and ANI reputation policy.

### Enterprise

Enterprise extends the same architecture with multi-location routing, role-based permissions, SLA tracking, advanced escalation rules, analytics, audit logs, external system orchestration, custom workflow definitions, CRM/ERP integration, customer memory systems, and advanced caller reputation policy.

No separate architecture should be created for Enterprise. Only additional capabilities should be enabled on the same orchestration core.

---

## 18. Database Direction

The current durable backend layer includes:

```text
clients
client_settings
calls
workflow_instances
operational_events
operator_action_tokens
client_plans
invoices
invoice_line_items
users
client_contacts
client_addresses
```

Important workflow-owned fields include:

```text
workflow_id
workflow_status
current_stage
last_event_type
last_event_at
notification_state
service_state
ownership_state
assigned_operator
assigned_team
```

Future workflow infrastructure may include workflow steps, workflow transitions, workflow assignments, workflow notes, notifications, booking records, and dispatch records.

Future caller/customer infrastructure may include customer records, customer phone numbers, customer addresses, customer interactions, and customer phone reputation.

---

## 19. UI Direction

The Client/Admin operational console must display backend truth.

Current and future surfaces may include Calls, Workflows, Timeline, Notifications, Bookings, Dispatch, Client Settings, Billing, Users, and Integrations.

The Calls view should not carry the whole product forever.

A future Workflow Detail surface should show intake state, triage state, caller identity state, caller reputation state, notification state, ownership state, booking state, dispatch state, service state, resolution state, allowed operator actions, event timeline, raw call reference, and operational diagnostics.

The UI must render backend-authoritative state rather than invent workflow truth locally.

---

## 20. Operational UI Color Canon

Operational colors remain fixed and globally consistent.

```text
green = successful operational progression / completed execution
red = escalation / failure / intervention / operational risk
gray = infrastructure / persistence / neutral system state
white or neutral = pending / inactive / undefined / unknown
deep pink = restrained brand accent only
```

Pink must not represent urgency, selection, completion, error, success, escalation, workflow state, service state, notification state, or queue state.

Color usage must preserve operational cognition over decoration.

---

## 21. Engineering Rule

Every new workflow feature must answer:

```text
What event does this create?
What workflow status can it change?
What current_stage can it change?
What lifecycle dimension does it belong to?
What table owns the truth?
What snapshot fields are updated?
What UI surface displays it?
What plan tier enables it?
What happens if it fails?
Does it require operator governance?
Does it require notification policy?
Does it affect caller identity or reputation?
Is it reusable across verticals?
Does it preserve backend-authoritative truth?
```

If a feature cannot answer these questions, it is not ready to be built.

---

## 22. Current Validated Platform Status

As of the May 19 orchestration checkpoint, Gosonic has validated:

```text
inbound call routing
Retell call analysis
ANI capture
call persistence
workflow creation
workflow snapshot fields
immutable operational event persistence
business SMS notification
caller SMS confirmation
operator acknowledgement link
ownership.acknowledged event
manual service progression
service-state transition validation
workflow-stage synchronization
service_state/current_stage/event_stage synchronization
ownership.assigned side-event on service assignment
workflow resolution
terminal workflow enforcement
/calls workflow snapshot exposure
workflow-first admin console rendering
priority/workflow separation
urgent does not automatically mean escalated
```

This validates Lite as an operational workflow foundation from intake through resolution.

---

## 23. Current Known Hardening Direction

Current known hardening priorities include authenticated operator identity, role-aware authorization, durable notification prerequisite enforcement, caller reputation infrastructure, Retell agent refinement, address verification, issue classification refinement, workflow detail surface, SLA intelligence, and aging/stale workflow detection.

These should be pursued after Lite remains stable, auditable, and structurally clean.

---

## 24. Canonical Statements

```text
A call may begin the workflow.
The workflow is the operational container.
The backend owns operational truth.
The database preserves operational truth.
The frontend renders operational truth.
Events preserve history.
Snapshot fields summarize current state.
Service progression, current_stage, and event_stage must remain synchronized.
Ownership is a first-class operational governance dimension.
Lite is the canonical foundation for future Pro, Pro+, and Enterprise orchestration.
```

That is the workflow architecture canon.
