# Gosonic Platform Schema Overview

## Purpose

The Gosonic platform database stores operational communication workflows, telephony metadata, workflow state, client configuration, and platform observability data.

The architecture is evolving from MVP persistence toward a scalable operational infrastructure model.

---

# Current Core Tables

## clients

Stores platform client accounts and routing ownership.

Primary responsibilities:
- inbound number ownership
- plan tier assignment
- operational status
- business identity
- timezone configuration

---

## client_settings

Stores runtime operational behavior configuration.

Examples:
- caller SMS enabled
- business SMS enabled
- escalation behavior
- workflow preferences
- future dispatch policies

---

## calls

Primary operational communication record.

Tracks:
- inbound/outbound call metadata
- urgency classification
- workflow outcome
- transcript persistence
- operational telemetry
- escalation metadata
- processing latency
- webhook lifecycle state

This table currently acts as the operational core of the MVP system.

---

# Planned Architecture Expansion

## workflow_instances

Future canonical workflow lifecycle table.

Will track:
- workflow state progression
- orchestration lifecycle
- assignment state
- operational ownership
- workflow completion

---

## operational_events

Immutable operational event stream.

Examples:
- call_started
- workflow_created
- sms_sent
- escalation_triggered
- booking_confirmed
- dispatch_completed

This becomes the foundation for:
- observability
- auditability
- lifecycle replay
- analytics
- enterprise orchestration

---

# Architectural Direction

The Gosonic platform is evolving toward:
- event-driven infrastructure
- operational workflow orchestration
- multi-tenant communication systems
- enterprise-grade observability
- durable lifecycle persistence