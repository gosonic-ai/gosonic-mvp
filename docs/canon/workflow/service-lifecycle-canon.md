# Gosonic Service Lifecycle Canon

## Purpose

The service lifecycle canon defines the authoritative operational progression of a service workflow after intake has been completed.

This canon exists to ensure:
- deterministic workflow behavior
- backend-authoritative orchestration
- operational auditability
- canonical transition enforcement
- reusable orchestration semantics across Lite, Pro, Pro+, and future enterprise plans

The service lifecycle is separate from:
- telephony lifecycle
- notification lifecycle
- acknowledgement lifecycle
- billing lifecycle

A workflow may progress operationally even while other lifecycle dimensions continue independently.

---

# Core Principles

1. Service progression is backend-authoritative.
2. Frontend surfaces render allowed actions only.
3. Invalid lifecycle transitions must be rejected.
4. Terminal workflows cannot be reopened.
5. Every lifecycle transition must persist immutable operational events.
6. Workflow state and service state are related but distinct.
7. Queue semantics derive from canonical workflow/service state.
8. Lifecycle semantics must remain reusable across verticals and plans.

---

# Lifecycle Dimensions

## Workflow State

Represents the overall orchestration condition of the workflow.

Examples:
- active
- resolved
- failed
- escalated
- paused

## Service State

Represents operational service progression.

Examples:
- triaged
- awaiting_dispatch
- scheduled
- assigned
- in_progress
- resolved

## Notification State

Represents communication progression.

Examples:
- pending
- business_sent
- caller_sent

## Acknowledgement State

Represents whether operational receipt acknowledgement has occurred.

Examples:
- unacknowledged
- acknowledged

Acknowledgement and service progression remain separate lifecycle dimensions.

---

# Current Lite Canonical Lifecycle

Current Lite service progression:

triaged
→ awaiting_dispatch
→ scheduled
→ assigned
→ in_progress
→ resolved

---

# Service State Definitions

## triaged

The intake has been analyzed and categorized.
The workflow is operationally active but has not entered dispatch progression.

## awaiting_dispatch

The workflow is operationally ready for scheduling or dispatch handling.

## scheduled

A service appointment or operational commitment has been scheduled.

## assigned

A technician or operational resource has been assigned.

## in_progress

Operational work is actively occurring.

## resolved

Operational work is considered completed and the workflow becomes terminal.

---

# Terminal States

The following workflow states are terminal:

- resolved
- failed

Terminal workflows:
- expose no forward actions
- cannot be reopened through standard operator progression
- remain immutable historical operational records

---

# Transition Enforcement

Lifecycle transitions must be enforced by the backend.

Frontend applications must not determine valid lifecycle progression independently.

Invalid transitions include:
- skipped transitions
- backward transitions
- reopening terminal workflows
- unauthorized transitions
- policy-restricted transitions

---

# Current Allowed Lite Transitions

Allowed transitions:

triaged → awaiting_dispatch
awaiting_dispatch → scheduled
scheduled → assigned
assigned → in_progress
in_progress → resolved

---

# Invalid Transition Examples

Rejected examples:

triaged → scheduled
scheduled → in_progress
resolved → scheduled
assigned → awaiting_dispatch

These transitions must return backend rejection responses.

---

# Queue Semantics

Queue semantics derive from canonical workflow/service state.

Examples:

- active + triaged → awaiting_service
- active + awaiting_dispatch → awaiting_service
- active + scheduled → active
- resolved + resolved → resolved
- failed + failed → failed

Queue semantics are operational abstractions, not authoritative workflow truth.

---

# Event Persistence

Every lifecycle transition must persist immutable operational events.

Examples:

- service.triaged
- service.awaiting_dispatch
- service.scheduled
- service.assigned
- service.in_progress
- workflow.resolved

Operational history must remain auditable and append-only.

---

# Governance Separation

Lifecycle progression and governance policy are separate concerns.

Lifecycle defines:
- what states exist
- what transitions are valid

Governance defines:
- who may perform transitions
- whether acknowledgement is required
- whether escalation blocks progression
- whether SLA conditions apply
- whether policy restrictions apply

---

# Future Expansion

Future lifecycle expansion may include:

- dispatch_pending
- technician_en_route
- awaiting_parts
- customer_confirmation_pending
- follow_up_required
- recurring_service
- cancelled
- warranty_review
- quality_assurance_review

Future expansion must preserve:
- deterministic transitions
- backend authority
- immutable event persistence
- canonical operational semantics

---

# Current Architectural Direction

The backend owns:
- lifecycle truth
- transition validation
- action eligibility
- immutable event persistence

The frontend renders:
- backend-authorized actions
- workflow visibility
- operational state presentation

This separation is foundational to Gosonic orchestration architecture.