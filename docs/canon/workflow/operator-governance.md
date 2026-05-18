# Gosonic Operator Governance Canon

## Purpose

Operator governance defines who may perform operational workflow actions, under what conditions, and how those actions are represented in the system.

This canon exists to prevent workflow controls from becoming arbitrary UI buttons. Every operator action must map to backend-authoritative workflow state, immutable event persistence, and operational auditability.

## Core Principles

1. Backend owns action authority.
2. Frontend renders allowed actions only.
3. Workflow state determines available actions.
4. Terminal workflows expose no forward actions.
5. Operator actions must persist immutable events.
6. Acknowledgement and service progression are separate lifecycle dimensions.
7. Governance policies may restrict actions later without changing frontend rendering logic.

## Current Lite Policy

For the current Lite workflow:

- Service progression may continue even if SMS acknowledgement has not occurred.
- Operator acknowledgement records receipt of the request.
- Operator acknowledgement does not automatically advance service state.
- Service progression remains manual and explicit.

## Current Lite Service Progression

triaged
→ awaiting_dispatch
→ scheduled
→ assigned
→ in_progress
→ resolved

## Future Governance Policies

Future orchestration policies may include:

- require acknowledgement before dispatch progression
- allow immediate dispatch progression
- escalate if unacknowledged after a defined interval
- restrict terminal resolution to specific roles
- restrict failure actions to platform admins or client admins
- distinguish operator, dispatcher, technician, and viewer capabilities
- apply tenant-specific workflow policies
- apply plan-tier-specific workflow policies

## Action Authorization Model

Every future action should answer:

- What workflow state allows this action?
- What role may perform it?
- Does acknowledgement matter?
- Does plan tier matter?
- What event is persisted?
- What snapshot fields are updated?
- What happens on failure?
- Is the action terminal?
- Is the action reversible?
- Is the action visible to the client?

## Acknowledgement Governance

Acknowledgement governance controls whether service progression is allowed before the business/operator has explicitly acknowledged receipt of the request.

Acknowledgement and service progression remain separate lifecycle dimensions.

### Current Lite Policy

Current Lite behavior allows service progression before acknowledgement.

Reason:
- operators may receive the request through SMS, phone, dashboard, or internal process
- acknowledgement confirms receipt but does not necessarily determine operational readiness
- service progression remains an explicit operator action

### Future Policy Options

Future client or plan-level policies may include:

1. `acknowledgement_optional`
   - service progression can begin immediately after intake and notification

2. `acknowledgement_required_before_dispatch`
   - service progression cannot move from `triaged` to `awaiting_dispatch` until acknowledgement is recorded

3. `acknowledgement_required_before_resolution`
   - workflow may progress operationally, but terminal resolution requires acknowledgement

4. `auto_escalate_if_unacknowledged`
   - workflow remains active but escalates if acknowledgement is not recorded within a configured SLA window

### Governance Rule Placement

Acknowledgement policy must be enforced in backend action generation before actions are exposed to the frontend.

The frontend must not decide whether acknowledgement is required.

Future enforcement belongs in the backend operator action eligibility layer.

## Current Implementation Direction

The backend returns `operator_actions` in the `/calls` response.

The frontend must render those actions without encoding workflow rules locally.

This establishes the foundation for policy-controlled orchestration.