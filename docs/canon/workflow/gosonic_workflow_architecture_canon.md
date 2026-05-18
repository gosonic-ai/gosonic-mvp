# Gosonic Workflow Architecture Canon v2
## Authoritative Workflow & Orchestration Standards — May 2026

---

# 1. Purpose

This document defines the canonical workflow architecture of the Gosonic platform.

It exists to preserve continuity across:
- workflow design
- backend orchestration
- event taxonomy
- service lifecycle progression
- operator governance
- client/admin interfaces
- Retell voice-agent behavior
- future Pro, Pro+, and Enterprise capabilities

This document is considered a source canon for how Gosonic understands, represents, advances, and observes operational workflows.

---

# 2. Foundational Principle

Gosonic is not a collection of call flows.

Gosonic is an operational communication infrastructure platform.

Every phone call, message, intake, acknowledgement, booking, dispatch action, escalation, service update, and resolution should be treated as part of a larger operational workflow.

A call may begin the workflow.

The workflow may continue after the call ends.

The platform must understand:
- what happened
- what state the work is in
- what still needs to happen
- who or what is responsible for the next operational step
- whether the workflow completed successfully
- whether the caller identity is known, partial, anonymous, restricted, or blocked
- whether the request is operationally actionable
- whether the workflow requires escalation, acknowledgement, scheduling, dispatch, or resolution

The platform exists to coordinate the operational work that follows communication activity.

---

# 3. Core Architecture Rule

Gosonic must be built around:
- events
- workflow state
- lifecycle semantics
- backend-authoritative truth
- durable operational records
- observable operational progression

It must not be built around screens.

The UI displays operational truth.

The backend owns operational truth.

The database preserves operational truth.

The voice agent initiates and enriches operational events, but it is not the entire system.

The workflow architecture must remain reusable across Lite, Pro, Pro+, and Enterprise capabilities.

---

# 4. Canonical Object Model

```text
Client
  └── Caller Identity / ANI Context
        └── Call
              └── Workflow Instance
                    ├── Operational Events
                    ├── Current Workflow State
                    ├── Service State
                    ├── Notification State
                    ├── Acknowledgement State
                    ├── Operator Actions
                    └── Timeline
```

A caller identity context may exist before a workflow is complete.

A phone number may exist before a caller is fully identified.

A call may exist before a workflow is actionable.

A workflow may exist after the call has ended.

A workflow may include:
- SMS notification
- caller confirmation
- operator acknowledgement
- booking
- dispatch
- technician assignment
- CRM/customer lookup
- address verification
- escalation policy
- resolution
- post-resolution audit history

The call is one operational surface.

The workflow is the broader operational container.

---

# 5. Canonical Lifecycle Dimensions

Gosonic workflows are not represented by a single status.

Operational reality is represented through layered lifecycle dimensions.

Canonical dimensions:

```text
telephony state
intake state
caller identity state
workflow state
notification state
acknowledgement state
service state
booking state
dispatch state
resolution state
```

Not every plan uses every dimension.

Lite currently emphasizes:
- telephony state
- intake state
- caller identity state
- workflow state
- notification state
- acknowledgement state
- service state
- resolution state

Pro adds stronger booking semantics.

Pro+ adds dispatch and assignment semantics.

Enterprise extends the same model with advanced orchestration policy, CRM/ERP integrations, multi-location routing, SLA governance, and custom workflow definitions.

---

# 6. Workflow Instance

A `workflow_instance` represents the operational job created from a call or communication event.

A workflow instance is not merely a call record.

It is the durable operational container for the work that follows.

Example:

A homeowner calls about no heat.

The system receives inbound ANI.

The voice agent captures the issue.

The backend classifies the request as urgent.

The call is analyzed.

A call record is persisted.

A workflow instance is created.

Operational events are appended.

Business and caller notifications are sent.

An operator may acknowledge receipt.

The service may progress through triage, dispatch, scheduling, assignment, active work, and resolution.

The call is complete.

The workflow may not be complete.

The caller may be known, partially known, anonymous, restricted, or blocked.

These distinctions are foundational.

---

# 7. Canonical Workflow State

Workflow state represents the overall orchestration condition of the workflow.

Current canonical workflow states include:

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

Workflow state is the current operational truth of the workflow as a whole.

It is separate from:
- telephony state
- caller identity state
- notification state
- acknowledgement state
- service state
- urgency
- queue state
- UI display label

Events are history.

State is current truth.

Both are required.

---

# 8. Canonical Service State

Service state represents the operational progression of the service request after intake.

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

Current canonical Lite progression:

```text
triaged
→ awaiting_dispatch
→ scheduled
→ assigned
→ in_progress
→ resolved
```

Service state is separate from workflow state.

A workflow may be active while service state is scheduled.

A workflow may be resolved when service state becomes resolved.

A workflow may fail if service state becomes failed.

Service progression must be backend-authoritative.

Invalid transitions must be rejected by the backend.

Terminal workflows must not expose forward actions.

---

# 9. Notification State

Notification state represents communication execution.

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

Notification state is separate from workflow state and service state.

A notification may succeed while service progression remains pending.

A notification may fail while the workflow remains active.

Notification events must be explicit and auditable.

Canonical notification events include:

```text
notification.business_sent
notification.business_failed
notification.caller_sent
notification.caller_failed
```

Future hardening rule:

External notifications should not be sent before required durable operational prerequisites have succeeded.

The preferred ordering is:
1. validate notification policy
2. persist call
3. create or confirm workflow
4. generate required operator/action tokens when applicable
5. send notifications
6. append notification events
7. update workflow snapshot state

This prevents external SMS from being sent without an auditable operational record.

---

# 10. Acknowledgement State

Acknowledgement state represents whether the business/operator has confirmed receipt of the request.

Acknowledgement is separate from service progression.

Current acknowledgement event:

```text
operator.acknowledged
```

Acknowledgement does not automatically mean:
- the service was scheduled
- the service was assigned
- the service was dispatched
- the workflow was resolved

Acknowledgement confirms operational receipt.

Future governance policies may require acknowledgement before certain service transitions.

The backend must own acknowledgement policy.

The frontend must render only backend-authorized actions.

---

# 11. Caller Identity State

Caller identity state is separate from workflow state.

Current canonical caller identity fields:

```text
caller_phone_source
caller_identity_status
caller_phone_verified
```

Current canonical caller phone sources:

```text
ani
spoken
manual
crm
unknown
```

Current canonical caller identity states:

```text
anonymous
partial
known
restricted
blocked
```

Current interpretation:

## anonymous

No meaningful caller identity has been established.

## partial

A valid phone identity exists, but caller/service identity is incomplete.

## known

Caller identity and required service request information satisfy operational completeness requirements.

## restricted

Caller may be operationally limited through governance, reputation, or policy.

## blocked

Caller routing may be denied through explicit operational policy.

ANI is authoritative inbound telephony metadata available before conversational extraction.

However:

```text
phone known
≠ caller known
≠ request valid
≠ workflow complete
≠ operationally trusted
```

These dimensions must remain explicitly separated throughout the workflow system.

---

# 12. Urgency, Priority, Escalation, and Queue State

Urgency is not workflow state.

Priority is not escalation.

Escalation is not simply urgency.

Canonical separation:

```text
Priority / Urgency = severity of the request
Workflow State = operational handling condition
Queue State = operational grouping for UI/work management
Escalation = intervention or exceptional handling state
```

A confirmed urgent request may still be:

```text
Workflow: Awaiting Service
Priority: Urgent
```

Escalated should be reserved for true operational intervention states.

Queue state is an operational abstraction derived from backend truth.

Queue state must not become the source of truth.

---

# 13. Operational Events

Every meaningful system action should produce an immutable operational event.

Events are:
- append-oriented
- historically meaningful
- operationally observable
- timeline-renderable
- audit-compatible
- orchestration-compatible

Canonical event examples include:

```text
call.started
call.completed
call.analyzed
call.failed

workflow.created
workflow.activated
workflow.stage_changed
workflow.status_changed
workflow.resolved
workflow.failed
workflow.cancelled
workflow.expired

intake.started
intake.completed
intake.incomplete
intake.corrected

triage.started
triage.completed
triage.urgent_detected
triage.standard_detected
triage.failed

notification.business_sent
notification.business_failed
notification.caller_sent
notification.caller_failed

operator.acknowledged

service.triaged
service.awaiting_dispatch
service.scheduled
service.assigned
service.in_progress
service.resolved
service.failed
```

Future caller identity and reputation events may include:

```text
caller.identity.detected
caller.identity.partial
caller.identity.verified
caller.reputation.watchlisted
caller.reputation.restricted
caller.reputation.blocked
caller.crm.matched
caller.address.prefilled
```

Events should not be overwritten.

They become the operational history.

The UI timeline should be derived from events.

---

# 14. Workflow Snapshot State

A workflow instance may contain current snapshot fields for efficient rendering and operational queueing.

Current snapshot-style fields include:

```text
workflow_status
current_stage
last_event_type
last_event_at
notification_state
service_state
```

Snapshot fields summarize current truth.

Operational events preserve historical truth.

Both are required.

Snapshot fields must be updated only through backend-authoritative workflow helpers.

Frontend surfaces must not invent workflow state independently.

---

# 15. Operator Actions

Operator actions must be backend-authoritative.

The backend determines:
- which actions are available
- whether the workflow is terminal
- whether acknowledgement is required
- whether a transition is valid
- whether a role or plan tier may perform the action
- what event is persisted
- what snapshot fields change

Frontend surfaces render allowed actions.

They do not define operational policy.

Current Lite operator action direction includes manual service progression through:

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

# 16. Plan Capability Layers

## Lite

Lite includes:

```text
voice intake
triage
call summary
ANI capture
caller identity semantics
caller phone verification semantics
business SMS notification
caller SMS confirmation
call persistence
workflow persistence
notification tracking
operator acknowledgement
manual service progression
workflow timelines
operational call timelines
workflow resolution
```

Current Lite terminal state may be:

```text
resolved
```

when manual service progression is completed.

Lite must remain narrow, stable, auditable, and excellent.

---

## Pro

Pro adds:

```text
calendar availability
booking requests
booking confirmation
structured availability logic
CRM/contact synchronization
customer confirmation
customer identity enrichment
address validation/prefill
workflow booking state
client-facing workflow controls
```

Typical terminal state may be:

```text
booked
```

or:

```text
resolved
```

depending on service lifecycle depth.

---

## Pro+

Pro+ adds:

```text
dispatch lifecycle
operator acknowledgement policy
technician assignment
dispatch status
service completion tracking
territory/routing intelligence
advanced escalation handling
SLA automation
ANI reputation policy
```

Typical terminal state:

```text
resolved
```

---

## Enterprise

Enterprise extends the same architecture with:

```text
multi-location routing
role-based permissions
SLA tracking
advanced escalation rules
analytics
audit logs
external system orchestration
custom workflow definitions
CRM/ERP integration
customer memory systems
advanced caller reputation policy
```

No separate architecture should be created for Enterprise.

Only additional capabilities should be enabled on the same orchestration core.

---

# 17. Database Direction

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

Current call identity fields include:

```text
caller_phone
caller_phone_source
caller_identity_status
caller_phone_verified
```

Future workflow infrastructure may include:

```text
workflow_steps
workflow_transitions
workflow_assignments
workflow_notes
notifications
booking_records
dispatch_records
```

Future customer/caller identity infrastructure may include:

```text
customer_records
customer_phone_numbers
customer_addresses
customer_interactions
customer_phone_reputation
```

Future caller reputation fields may include:

```text
phone_number
client_key
reputation_status
total_inbound_calls
failed_call_count
abandoned_call_count
confirmed_call_count
last_call_at
last_failed_at
blocked_reason
created_at
updated_at
```

ANI reputation should evolve as operational caller infrastructure rather than isolated spam blocking.

---

# 18. UI Direction

The Client/Admin operational console should display backend truth.

Current and future surfaces may include:

```text
Calls
Workflows
Timeline
Notifications
Bookings
Dispatch
Client Settings
Billing
Users
Integrations
```

The Calls view should not carry the whole product.

Calls are one operational surface.

A future Workflow Detail surface should show:
- intake status
- triage status
- caller identity status
- caller reputation status
- notification status
- acknowledgement status
- booking status
- dispatch status
- service state
- resolution status
- allowed operator actions
- event timeline
- raw call reference
- operational diagnostics

The UI must render backend-authoritative state rather than inventing workflow truth locally.

---

# 19. Color and Status Canon

Operational colors remain fixed and globally consistent.

```text
green = successful operational progression / completed operational execution
red = escalation / failure / intervention / operational risk
gray = infrastructure / persistence / neutral system state
white or neutral = pending / inactive / undefined / unknown
deep pink = restrained brand accent only
```

Pink must not represent:
- urgency
- selection
- completion
- error
- success
- escalation
- workflow state
- service state
- notification state
- queue state

Color usage must preserve semantic operational clarity over decorative styling.

---

# 20. Engineering Rule

Every new workflow feature must answer these questions before implementation:

```text
What event does this create?
What workflow state can it change?
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

# 21. Current Validated Platform Status

As of the May 18 checkpoint, Gosonic has validated:

```text
inbound call routing
Retell inbound dynamic variables
ANI capture
caller phone source tracking
caller phone verification semantics
caller identity status classification
call.started handling
call.analyzed handling
call persistence
workflow creation
workflow snapshot fields
immutable operational event persistence
notification delivery
business SMS
caller SMS
operator acknowledgement link
operator acknowledgement event
manual service progression
service-state transition validation
workflow resolution
/calls workflow snapshot exposure
workflow-first admin console rendering
priority/workflow separation
urgent does not automatically mean escalated
```

This validates Lite as an operational workflow foundation from intake through resolution.

---

# 22. Current Known Hardening Direction

Current known hardening priorities include:

```text
notification sending should be gated behind durable persistence prerequisites
caller reputation infrastructure should be added carefully after identity semantics
Retell agent behavior should be refined for address correction and issue classification
operator controls should remain backend-authoritative
workflow details should eventually become a first-class operational surface
```

External notifications should not produce orphaned operational effects.

Caller identity should continue evolving into:
- repeat-caller recognition
- CRM matching
- address prefill
- ANI reputation
- restricted routing
- VIP handling

---

# 23. Canonical ANI Principle

```text
ANI is authoritative operational metadata.

However:

phone known
≠ caller known
≠ request valid
≠ workflow complete
≠ operationally trusted
```

This is a permanent workflow architecture rule.

---

# 24. Canonical Statement

Gosonic workflows are event-driven operational lifecycles initiated by communication activity.

A voice call may begin the workflow, but the platform exists to:
- track
- coordinate
- enrich
- notify
- acknowledge
- schedule
- dispatch
- escalate
- resolve
- audit

the operational work that follows.

The product should scale from simple intake notification to enterprise-grade operational orchestration without changing its core architectural philosophy.

That is the workflow canon.
