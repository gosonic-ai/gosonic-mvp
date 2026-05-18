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

## Current Implementation Direction

The backend returns `operator_actions` in the `/calls` response.

The frontend must render those actions without encoding workflow rules locally.

This establishes the foundation for policy-controlled orchestration.