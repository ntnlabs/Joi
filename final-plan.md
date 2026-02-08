# Joi Development Plan: Layered Approach

This document outlines a layered development strategy for Joi, starting with a Minimum Viable Product (MVP) and progressively adding functionality and security measures. This approach aims to deliver core functionality quickly and manage complexity effectively.

## Overall Strategy

The project will proceed in distinct layers, focusing on getting core components functional and verified before moving to more advanced features. This "MVP-first" approach allows for iterative development and refinement.

## Layer 1: The Simple Responder (MVP)

**Core Goal:** Joi can receive a message via Signal and respond with a simple, LLM-generated answer. This layer proves the fundamental communication pipeline and LLM interaction.

**Simplified Architecture:**

*   **Mesh VM:**
    *   Runs the `signal-cli` integration (as defined in `dev-notes.md`).
    *   Implements a basic Python proxy (`mesh/proxy/server.py`, `mesh/proxy/routes/inbound.py`, `mesh/proxy/routes/outbound.py`) to receive messages from `signal-cli` and forward them to the Joi VM.
*   **Joi VM:**
    *   Runs Ollama with the Llama 3.1 8B LLM.
    *   Implements a simple Python API server (`joi/api/server.py`, `joi/api/routes/inbound.py`) that:
        *   Receives incoming messages from the Mesh VM.
        *   Passes the message to the local Ollama LLM.
        *   Returns the LLM's response to the Mesh VM for delivery back to the user via Signal.

**Explicit Exclusions for MVP:**

*   **No Persistent Memory:** Joi will not retain conversation history or a long-term knowledge base. Each interaction is stateless.
*   **No External System Integration:** No connection to openHAB, Zabbix, calendars, or any other external systems. The "System Channel" is not implemented at this stage.
*   **Simplified Security Layer:** The full "Protection Layer" (complex rate limits, circuit breakers, extensive validation) from `system-channel.md` is not implemented. Basic air-gapped network security for the Joi VM is assumed.
*   **No LLM Service VMs:** No separate VMs for image generation, web search, TTS, etc.
*   **No Policy Engine:** The advanced `joi/policy/engine.py` is not implemented, beyond basic input/output validation within the API layer if deemed critical for initial stability.

**Key Implementation Steps (from `dev-notes.md`):**

1.  **Mesh proxy basics:** Accept requests, validate, forward.
2.  **Joi API basics:** Receive requests, call Ollama, respond.
3.  **Signal integration:** `signal-cli` daemon communication.

### Workload Estimates & Hardware Requirements

*   **Workload:** ~5-10 person-days.
*   **Hardware:**
    *   **Mesh VM:** Minimal resources (1-2 vCPU, 2GB RAM).
    *   **Joi VM:** Moderate resources (4-8 vCPU, 8-16GB RAM) and a **GPU** for Ollama/LLM inference.

---

## Layer 2: Adding Memory

**Core Goal:** Joi retains conversation history and can leverage a simple knowledge base for contextual responses.

**Enhancements:**

*   **Memory Store Implementation:** Implement the `joi/memory/store.py` using SQLCipher as planned.
*   **Contextual LLM Interaction:** Modify the Joi API and Agent to:
    *   Store incoming and outgoing messages in the memory store.
    *   Retrieve relevant past conversation history or basic knowledge snippets to provide more contextual prompts to the LLM.
*   **Basic Knowledge Base:** Implement a simple mechanism for Joi to ingest a small, static set of documents into its memory store for basic Q&A.

### Workload Estimates & Hardware Requirements

*   **Workload:** ~5-10 person-days.
*   **Hardware:** Same as Layer 1.

---

## Layer 3: Implementing the System Channel (Generic External Integration)

**Core Goal:** Joi can securely interact with various external systems via the generic "System Channel" architecture.

**Enhancements:**

*   **Generic System Channel API:** Implement the `POST /api/v1/system/event` (inbound) and `POST /api/v1/action` (outbound) endpoints as detailed in `system-channel.md`.
*   **Source Registry Integration:** Implement the source registration and configuration from `system-channel.md`.
*   **Initial External System Integration:** Integrate with one or two simple external systems (e.g., a basic read-only openHAB sensor for presence, or a simple calendar integration) to prove the System Channel's functionality.
*   **Policy Engine Integration (Basic):** Implement the core validation rules for the System Channel within `joi/policy/engine.py` (e.g., source validation, mode checks, allowed event types/actions).

### Workload Estimates & Hardware Requirements

*   **Workload:** ~10-20 person-days.
*   **Hardware:** Same as Layer 1. Resource usage on Joi VM might slightly increase with more active integrations.

---

## Layer 4: Full Protection Layer and Advanced Capabilities

**Core Goal:** Implement the robust, LLM-independent Protection Layer and integrate advanced LLM services.

**Enhancements:**

*   **Full Protection Layer:** Implement all components of the "Protection Layer" as defined in `system-channel.md`:
    *   Rate limiters, circuit breakers.
    *   Comprehensive input and output validation.
    *   Replay protection.
    *   Watchdog processes.
    *   Emergency stop mechanisms.
*   **Advanced Policy Engine:** Implement the full range of policy enforcement, including LLM decision verification, content policies, and dynamic access controls (e.g., for departmental separation).
*   **LLM Service VMs:** Integrate with separate LLM service VMs (e.g., `imagegen`, `websearch`) via the System Channel, enabling extended capabilities.
*   **Robust Error Handling & Logging:** Comprehensive logging and error handling across all layers.

### Workload Estimates & Hardware Requirements

*   **Workload:** ~20-40 person-days.
*   **Hardware:**
    *   **Mesh VM:** Same as previous layers.
    *   **Joi VM:** Same as previous layers, but potentially higher RAM/CPU depending on the complexity of protection mechanisms and policy rules.
    *   **LLM Service VMs:** Additional VMs, potentially requiring their own **GPUs** for specific services like image generation or video generation, as detailed in `system-channel.md`.

---

## Communication Platform Decision (Future Consideration)

The decision regarding the ultimate secure mobile communication platform (Silentel, Bittium, SINA) will be made once the core Joi functionality is proven. The MVP will rely on the current Signal integration. Transitioning to a certified platform would involve replacing the `signal-cli` integration with the chosen vendor's secure gateway/SDK in the Mesh VM, likely exposing a similar internal API for Joi to interact with.
