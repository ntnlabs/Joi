# Joi Development Plan: Layered Approach

This document outlines a layered development strategy for Joi, starting with a Minimum Viable Product (MVP) and progressively adding functionality and security measures. This approach aims to deliver core functionality quickly and manage complexity effectively.

## Overall Strategy

The project will proceed in distinct layers, focusing on getting core components functional and verified before moving to more advanced features. This "MVP-first" approach allows for iterative development and refinement.

---

## Layer 0: Core Infrastructure

This foundational layer is a prerequisite for all other layers and must be completed before application development begins. It covers the core security, communication, and OS-level hardening required by the architecture documents.

**Key Components & Tasks:**

*   **Nebula Mesh Setup:**
    *   Create a Nebula Certificate Authority (CA).
    *   Generate and securely deploy certificates for all initial nodes (Mesh VM, Joi VM).
    *   Configure the Nebula lighthouse on the Mesh VM.
    *   Implement firewall rules to ensure only Nebula traffic is allowed between VMs.
*   **Encrypted Disk Setup (LUKS):**
    *   Configure the Joi VM with a LUKS-encrypted primary disk.
    *   Establish the procedure for manual unlock via console access on every boot.
*   **`signal-cli` Hardening:**
    *   Create a dedicated `signal` Linux user with minimal permissions.
    *   Configure `signal-cli` to run in daemon mode using the JSON-RPC socket.
    *   Create and enable a systemd service file to manage the `signal-cli` daemon.
    *   Secure the socket with appropriate file permissions (e.g., 0660) and group ownership.
*   **OS-Level Isolation:**
    *   Create the initial set of Linux users and groups required for channel-based knowledge isolation, as documented in `Joi-architecture-v2.md`.
    *   Configure initial sudoers rules and Cgroup policies for subprocess isolation.

### Workload Estimates & Hardware Requirements

*   **Workload:** 3-5 person-days.
*   **Hardware:**
    *   **Mesh VM:** Minimal resources (1-2 vCPU, 2GB RAM).
    *   **Joi VM:** Base VM setup, no GPU required for this layer.

### PoC Branch: Joi with Wind (Impulse System)

This PoC aims to demonstrate Joi's proactive, 'living' behavior in a controlled manner.

*   **PoC Goal:** Joi will proactively send a message based on a simplified, dynamic `impulse_score`, with essential guardrails to prevent runaway behavior.

*   **Architecture & Workflow:**
    1.  This PoC builds on the **Layer 1 (Simple Responder)** and its minimal Policy Engine.
    2.  **Use SQLCipher for State:** The PoC will use a `SQLCipher` database from the start to store persistent state, avoiding file-based race conditions. The `system_state` table (from `memory-store-schema.md`) will be used to track `last_interaction` and `proactive_messages_sent_today`.
    3.  **Wind Scheduler:** A background thread will trigger an impulse check periodically.
    4.  **Impulse Calculation:** A simplified `calculate_impulse_score` function will run, using factors like `silence_factor` and `entropy_factor`.
    5.  **Guardrails & Threshold Check:** Before sending, the impulse check must pass two conditions:
        *   The calculated score exceeds the `IMPULSE_THRESHOLD`.
        *   A hard daily limit for proactive messages (e.g., max 5 per day) has not been reached. This limit is checked against the SQLCipher DB.
    6.  **Message Generation:** If the checks pass, the LLM generates a proactive message, which is then sent to the user. Every impulse check and its score will be logged for tuning.

*   **Workload Estimates & Hardware Requirements:**
    *   **Workload:** 5-8 person-days (includes SQLCipher setup).
    *   **Hardware:** Same as Layer 1 (Mesh VM, Joi VM with GPU).

*   **Success Criteria:**
    *   Joi successfully sends at least one proactive message in a 24-hour period.
    *   Joi does not exceed the daily limit of proactive messages.
    *   The `system_state` database is correctly updated after each interaction.

---

## Layer 1: The Simple Responder (MVP)

**Core Goal:** Joi can receive a message via Signal and respond with a simple, LLM-generated answer, protected by essential guardrails. This layer proves the fundamental communication pipeline and LLM interaction.

**Architecture:**

*   **Mesh VM:** Runs the hardened `signal-cli` daemon and a basic Python proxy.
*   **Joi VM:** Runs Ollama, the Python API server, and a minimal Policy Engine.
*   **Communication:** All traffic between VMs is over the secure Nebula mesh.

**Key Implementation Steps (from `dev-notes.md` & `findings.txt`):**

1.  **Mesh Proxy & Security:** Implement the Python proxy, including:
    *   **Sender-to-Channel Validation:** Ensure incoming messages originate from an authorized sender for the given channel.
    *   **Unknown Sender Protection:** Drop messages from unknown senders as per `policy-engine.md`.
2.  **Joi API Basics:** Implement the Python API to receive requests from the Mesh VM.
3.  **Minimal Policy Engine:** Create a basic `policy/engine.py` that is called by the Joi API on every incoming request to enforce:
    *   A global rate limit on incoming messages (e.g., 120/hour).
    *   Input size validation to prevent overly long messages (e.g., 4096 characters).
4.  **LLM Integration:** Connect the API to the Ollama backend, including robust error handling for timeouts or failures.
5.  **Health Check Endpoint:** Add a simple `/health` endpoint to both the Mesh and Joi API servers.
6.  **Signal Integration:** Connect the Mesh proxy to the hardened `signal-cli` daemon socket.

**Key Exclusions for this Layer:**

*   **No Persistent Memory:** Interactions are stateless. The full memory store is in Layer 2.
*   **No Generic System Channel:** No integration with external systems like openHAB.
*   **No Advanced Protection:** Excludes complex features like circuit breakers, replay protection, and the full `system-channel.md` generic interface.

### Workload Estimates & Hardware Requirements

*   **Workload:** 5-10 person-days.
*   **Hardware:**
    *   **Mesh VM:** Minimal resources (1-2 vCPU, 2GB RAM).
    *   **Joi VM:** Moderate resources (4-8 vCPU, 8-16GB RAM) and a **GPU** for Ollama/LLM inference.

---

## Layer 2a: Conversation Context & State

**Core Goal:** Give Joi a short-term memory of the ongoing conversation and a persistent state.

**Enhancements:**

*   **SQLCipher Integration:** Fully integrate the `SQLCipher` database into the Joi API.
*   **Conversation History:**
    *   Store every incoming and outgoing message in a `conversation_history` table.
    *   Before calling the LLM, retrieve the last N messages to provide conversational context.
*   **State Management:**
    *   Implement `Quiet Hours` system from `agent-loop-design.md`.
    *   Implement `Behavior Mode Toggle` (`companion` vs `assistant`).

### Workload Estimates & Hardware Requirements

*   **Workload:** 5-7 person-days.
*   **Hardware:** Same as Layer 1.

---

## Layer 2b: Knowledge Retrieval (RAG)

**Core Goal:** Enable Joi to answer questions by retrieving information from a dedicated knowledge base of documents.

**Enhancements:**

*   **Document Ingestion:**
    *   Create a script or process to ingest text documents, split them into chunks, and store them.
*   **Knowledge Retrieval (via `joi-retrieve`):**
    *   Develop the `joi-retrieve` binary (in Rust or Go, as per `dev-notes.md`). This binary will be responsible for accessing the knowledge base securely.
    *   For the initial implementation in this layer, `joi-retrieve` will use a keyword-based search like **SQLite FTS5**, as recommended in the findings, before evolving to use vector embeddings.
    *   The main Joi process will call this binary as a subprocess, enforcing isolation.
*   **RAG Prompting:**
    *   Modify the Joi agent to:
        1.  Detect when a user's question requires knowledge retrieval.
        2.  Invoke the `joi-retrieve` binary to search the knowledge base.
        3.  Inject the retrieved document chunks into the LLM prompt along with the user's question.

### Complication: RAG Complexity
*   This layer is a significant step up in complexity. It requires careful management of the LLM's context window (e.g., Llama 3.1 8B's 8K limit) to fit the system prompt, conversation history, and retrieved knowledge.
*   Running both the main LLM and a potential embedding model (if not using FTS5) on a single GPU can be resource-intensive.

### Workload Estimates & Hardware Requirements

*   **Workload:** 15-20 person-days.
*   **Hardware:** Same as Layer 1. GPU VRAM will be a primary constraint to monitor.

---

## Layer 3: Implementing the System Channel (Generic External Integration)

**Core Goal:** Joi can securely interact with various external systems via the generic "System Channel" architecture.

**Enhancements:**

*   **Generic System Channel API:** Implement the `POST /api/v1/system/event` (inbound) and `POST /api/v1/action` (outbound) endpoints as detailed in `system-channel.md`.
*   **Source Registry Integration:** Implement the source registration and configuration from `system-channel.md`.
*   **Initial External System Integration:** Integrate with one or two simple external systems (e.g., a basic read-only openHAB sensor for presence).
*   **File Upload Processing:** Implement the secure file upload handling mechanism as documented in `Joi-architecture-v2.md`.
*   **Policy Engine Integration (Basic):** Implement the core validation rules for the System Channel within `joi/policy/engine.py`.
*   **Configuration & Group Sync:**
    *   Implement a secure mechanism for **Config Sync** between the Mesh and Joi VMs.
    *   Implement **Group Membership Monitoring** to ensure the Signal group for critical alerts is correctly configured.

### Workload Estimates & Hardware Requirements

*   **Workload:** ~10-20 person-days.
*   **Hardware:** Same as Layer 1. Resource usage on Joi VM might slightly increase with more active integrations.

---

## Layer 4: Full Protection Layer and Advanced Capabilities

**Core Goal:** Implement the robust, LLM-independent Protection Layer, advanced security features, and integrate extended capabilities via LLM Service VMs.

**Enhancements:**

*   **Full Protection Layer Implementation:** Implement all advanced components of the Protection Layer:
    *   Circuit breakers.
    *   Replay protection.
    *   The high-reliability Mesh Watchdog.
    *   Emergency stop mechanisms.
    *   IoT Flood Protection.
*   **Advanced Policy Engine:**
    *   Implement the full range of policy enforcement, including LLM decision verification and content policies.
    *   Implement the **Channel-Based Knowledge Isolation** model (Linux users/groups, Cgroups) for secure multi-departmental use.
*   **LLM Service VMs:** Integrate with separate LLM service VMs (e.g., `imagegen`) via the System Channel.
*   **Maintenance USB Key System:** Implement the secure mechanism for performing maintenance tasks on the Joi VM via a physically present, encrypted USB key.

### Security Verification

This layer includes a dedicated and budgeted effort for rigorous security testing, which is as important as the implementation itself.

*   **Threat Model & Test Harness:** Develop a comprehensive threat model and an automated test harness for security features (10-15 person-days).
*   **Automated Adversarial Testing:** Implement automated tests to probe for weaknesses in the Protection Layer, including prompt injection, rate limit exhaustion, and validation bypasses (10-20 person-days).
*   **Manual Red-Teaming:** Conduct a manual red-team exercise to simulate a determined attacker attempting to compromise the system (5-10 person-days).
*   **Remediation:** Budget for fixing all findings from the verification process (5-10 person-days).

### Workload Estimates & Hardware Requirements

*   **Workload:** 50-85 person-days (includes implementation and security verification).
*   **Hardware:**
    *   Mesh VM & Joi VM.
    *   **LLM Service VMs:** Additional VMs, potentially requiring their own **GPUs** for specific services like image generation.

---

## Communication Platform Decision (Future Consideration)

The decision regarding the ultimate secure mobile communication platform (Silentel, Bittium, SINA) will be made once the core Joi functionality is proven. The MVP will rely on the current Signal integration. Transitioning to a certified platform would involve replacing the `signal-cli` integration with the chosen vendor's secure gateway/SDK in the Mesh VM, likely exposing a similar internal API for Joi to interact with.
