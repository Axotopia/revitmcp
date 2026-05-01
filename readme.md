# Axoworks Revit MCP Logic Engine for AnythingLLM

> [!CAUTION]
> **Experimental MVP / Proof of Concept:** This project is an early-stage technical experiment in "Intent Engineering." It is a work-in-progress that requires significant fine-tuning, prompt calibration, and the addition of more robust tools to the codebase before it can be considered production-ready.

---

## 🌪️ Breaking the Walled Garden: From Closed Loops to Sovereign RAG

The AEC industry is currently limited by "Closed Loop" AI assistants—tools like Autodesk Assistant that operate strictly within the proprietary boundaries of a single software instance. Crucially, these built-in assistants do not allow outside sources to be integrated via RAG (Retrieval-Augmented Generation). This means you cannot natively audit your Revit model against the actual realities of your project: your specific contracts, local building codes, structural specifications, and zoning ordinances.

**The Axoworks Revit MCP Engine** is an experimental middleware designed to break this loop. By bridging the gap between the **Autodesk Revit MCP Server** and **AnythingLLM**, we enable robust, open-loop architectural auditing against your own document libraries.

### Core Objectives

1.  **Bring Your Own Knowledge (RAG):** Move beyond simple geometry retrieval. Extract Revit element data and query it directly against real business and legal documents stored securely in your own vector database.
2.  **LLM Freedom & Data Sovereignty:** Maintain total control over your tech stack. By leveraging AnythingLLM's pre-built infrastructure, you have the free choice to use any LLM—whether running a sovereign local model to protect proprietary design data, or connecting to a powerful external API. 
3.  **Intent Engineering:** By offloading vector storage and LLM inference to AnythingLLM, we can focus 100% of our development effort on refining the Python middleware and building robust, deterministic audit Tools.

---

## ⚠️ The Single-Thread Bottleneck (And How We Handle It)

As an early proof of concept, this engine must navigate a major architectural limitation of the host Autodesk Revit MCP Server pipeline: **single-threaded API access**.

Currently, all Revit database queries are forced to queue and execute sequentially on the main UI thread. When an asynchronous LLM agent issues rapid-fire, complex queries (or initiates retry loops), it creates a severe bottleneck that can easily deadlock the Autodesk pipe. We are hopeful that Autodesk will address this multi-threading limitation in future updates.

**The Mitigation:** To prevent these lockups and keep the Revit MCP from crashing, this middleware routes all traffic through a strict **Governance Layer (`governor.py`)**. This governor manages request deduplication, provides asynchronous heartbeats to impatient LLM agents, and audits payloads to shield the main thread from overload.

---

### 🧪 Baseline Model & Expected Performance

> [!NOTE]  
> **Current Testing Baseline:** This middleware is currently optimized and tested using the **Qwen 3.6 (35B)** model running locally via Ollama (`qwen3.6:35b-a3b-bf16`). 

Because this engine relies heavily on strict MCP tool-calling sequences and rigorous adherence to the workspace system prompt, your choice of LLM dictates the success rate of the audits.

* **Recommended:** Models in the 30B+ parameter range (like Qwen or deep reasoning models) are highly recommended. They possess the necessary context window and instruction-following capabilities to handle complex JSON schema routing reliably.
* **Warning (Smaller Models):** Smaller models (e.g., 7B–8B parameters) often fail to pass required arguments (such as omitting `"searchScope": "AllViews"`) or get stuck in infinite retry loops. This poor tool-calling behavior directly increases the risk of deadlocking the Revit main thread. Performance and outcomes will vary significantly if you deviate from the baseline model class.

---

## 🏗️ Architecture & Protocols

This application serves as a high-performance Python MCP (Model Context Protocol) Proxy layer, bridging AnythingLLM with the Autodesk Revit server to ensure stability, translation, and speed.

*   **Revit Bridge (Internal):** Uses **JSON-RPC 2.0 over NDJSON** to communicate with the Autodesk Revit MCP server via Windows Named Pipes. This ensures a low-latency connection to the BIM environment.
*   **MCP Proxy (External):** Exposes a **native MCP Server via standard I/O (stdio)**, allowing AnythingLLM to connect to it directly as a first-class citizen, completely bypassing buggy "Agent Skills" plugins.
*   **Coordinate Translation Layer:** Silently intercepts MCP responses from Revit and translates Global Z coordinates (Internal Origin) to Project Z coordinates (Project Base Point), preventing LLM hallucination.
*   **Governance Middleware (Request Governor):** Intercepts all traffic between the MCP Proxy and the Revit pipe. It provides **Request Deduplication**, **Asynchronous Heartbeats** to prevent agent timeouts, and **Payload Auditing** to block dangerous queries before they reach Revit's main thread.
*   **Custom Audit Tools:** Injects deterministic logic tools (e.g., Septic Setbacks) directly into the MCP payload, sitting seamlessly alongside native Revit tools.

---

## 🛑 Prerequisites: Windows System Setup

Before setting up the engine, you need the core technologies installed on your Windows machine.

### 1. Install Python (Windows)
If you do not already have Python installed:
1. Go to the official Python website: [https://www.python.org/downloads/windows/](https://www.python.org/downloads/windows/)
2. Download the latest **Windows installer (64-bit)** for Python 3.10 or higher.
3. Run the installer.
4. **CRITICAL STEP:** At the very bottom of the first installation screen, you MUST check the box that says **"Add python.exe to PATH"** before clicking "Install Now".
5. Once installed, open your Command Prompt (or PowerShell) and type `python --version` to verify it installed correctly.

### 2. Install Ollama
1. Download and install Ollama from [https://ollama.com/](https://ollama.com/).
2. Open your terminal and pull the Qwen model to your local machine:
   ```bash
   ollama pull qwen3.6:35b
   ```
   *(Note: You will select and configure this model directly inside AnythingLLM's settings).*

---

## 🛠️ Installation & Virtual Environment Setup

To keep this project isolated and prevent dependency conflicts, we use a Python Virtual Environment (`venv`).

### 1. Create the Virtual Environment
Inside your project folder, create the isolated environment by running:
```bash
python -m venv venv
```

### 2. Activate the Virtual Environment
You must activate the environment every time you want to work on or run the engine.
* **Command Prompt:** `.\venv\Scripts\activate.bat`
* **PowerShell:** `.\venv\Scripts\Activate.ps1`
*(Note: If PowerShell gives you an "Execution Policy" error, run this command as Administrator first: `Set-ExecutionPolicy Unrestricted -Scope CurrentUser`, then try activating again).*

### 3. Install Dependencies
With the `(venv)` active, install the required packages:
```bash
pip install -r requirements.txt
```

---

## ⚙️ Configuration & Deployment

The engine uses a `.env` file to ensure smooth deployment across different firms and networks without breaking. Before running, copy the `.env.example` file (if provided) or create a `.env` file in the root directory.

### Pipe Auto-Discovery
Autodesk Revit generates dynamic Named Pipes (e.g., `\\.\pipe\revit-mcp-1a2b3c...`) that change every time the software starts. 
* You do **not** need to hardcode the exact pipe name. 
* The engine uses a robust PowerShell auto-discovery script to scan the host machine for any active pipe starting with the `REVIT_PIPE_PREFIX` defined in the `.env` file (default: `\\.\pipe\revit-mcp`). It will automatically connect.

### Bypassing API Ports
Because the system now runs as a native MCP standard I/O proxy, it does not bind to any network ports (e.g., `8000`). It uses stdin/stdout, completely eliminating port collisions on strict firm networks.


### Governance Layer Tuning
The governance layer prevents Revit deadlocks from rapid LLM retries. You can tune these in `.env`:
* `GOVERNOR_HEARTBEAT_THRESHOLD_S=25`: Seconds before sending a "still processing" response to prevent client timeout.
* `GOVERNOR_CACHE_TTL_S=10`: How long completed results stay in the deduplication cache.
* `GOVERNOR_DANGEROUS_CATEGORIES`: Comma-separated list of categories that require filters when geometry is requested (e.g., `Generic Models`).

---

## 🚀 Running the Engine

You **do not** need to manually start a server! Because this operates as a native MCP server, AnythingLLM automatically manages its lifecycle.

1. Once configured in AnythingLLM (see below), AnythingLLM will spawn `main_mcp.py` in the background automatically when you open a workspace.
2. The proxy will transparently connect to the Autodesk Revit Named Pipe, discover the native tools, inject its custom audit tools, and start proxying traffic.

---

## 🧪 Available Tools

The MCP Proxy dynamically exposes the following tools directly to your AnythingLLM agent:

### 1. Native Revit Tools (Pass-through)
* `query_model`, `get_element_data`, `zoom_to_elements`, etc. 
* **Key Feature:** All geometry coordinates (`boundingBox`, `geometry` points) returned by these tools are automatically intercepted and translated from Global Z to Project Z in transit.

### 2. Custom Audit Tools
* `axo_audit_septic`: (Placeholder) Septic setback compliance.
* `axo_audit_energy`: (Placeholder) Energy envelope checks.
* `axo_audit_wwr`: (Placeholder) Window-to-Wall ratio compliance.
* `axo_audit_floor_area`: Queries floor area data from the active Revit model. Extracts Rooms (`OST_Rooms`), retrieves per-room area/name/number/level via `get_element_data`, and aggregates by level. Supports optional `level_names` filter (e.g., `["FP1.GARAGE", "FP2.ADU"]`) and `include_room_details` toggle.
* `axo_audit_lot_area`: Calculates the lot area (area enclosed by property lines) from the active Revit model. Queries `OST_SiteProperty` elements with fallback strategies (`get_elements_by_category`, `OST_Site`). Extracts polyline/polygon geometry from element curves and bounding boxes, then computes enclosed area using the **shoelace formula** (deterministic math, no LLM). Supports `area_unit` parameter (`"sqft"`, `"acres"`, or `"both"`).
* `axo_audit_lot_coverage`: Calculates the lot coverage percentage — `(Building Footprint ÷ Total Lot Area) × 100` — for the active Revit model. This single tool replaces what would otherwise require multiple LLM-orchestrated tool calls. **Queries three data sources deterministically:**
  1. **OST_SiteProperty** → lot area via shoelace formula
  2. **OST_Floors** → building footprint from floor element areas
  3. **OST_Areas** → additional covered areas (decks, patios, etc.)
  Returns a structured breakdown with lot area (sq ft/acres), building footprint per element, covered areas, and two coverage percentages (building-only and total). All math is deterministic — no LLM involvement. This tool eliminates the multi-step fragility that caused the Qwen agent to fail on the same query. Supports `area_unit` (`"sqft"`, `"acres"`, or `"both"`) and `include_details` (toggle per-element breakdown).
* `axo_audit_setback`: Calculates the closest distance from building exterior walls to property lines (setback/proximity analysis). Queries `OST_Walls` for exterior walls and `OST_SiteProperty` (with fallbacks to `get_elements_by_category('Property Lines')` and `OST_Site`). Extracts bounding box extents and curve geometry from both element sets, then computes minimum perpendicular distances per side (North, South, East, West) using the point-to-line-segment distance formula (deterministic math, no LLM). Returns distances in feet & inches (configurable via `output_unit`: `"ft_in"`, `"ft"`, or `"in"`). Identifies the overall closest setback and provides property line segment details (start/end coordinates, orientation, length). Equivalent to Revit's built-in property line proximity analysis.

*(Note: These custom tools bypass LLM math errors by executing local calculations and only using the LLM for final narrative summaries).*

---

## 🛡️ Governance Layer: Main Thread Protection

Revit enforces **single-threaded API access**. All operations related to reading and updating the model are queued and executed sequentially on the main UI thread. AnythingLLM’s agent executor is asynchronous and "impatient"—if a complex Revit query takes longer than 30 seconds, the agent assumes failure and initiates rapid-fire retries. 

Without governance, these retries flood the Revit `ExternalEvent` queue and permanently deadlock the host. The Axoworks engine implements a triple-layer governor to decouple the asynchronous agent from the synchronous host:

1.  **Request Deduplication (The State Manager):** 
    The engine tracks the signature (method + parameters) of every in-flight request. If a duplicate retry arrives while the host is still processing, the governor silently coalesces the request and waits for the original task to finish instead of forwarding a second trigger to Revit.

2.  **Asynchronous Heartbeat (Timeout Mitigation):** 
    If a Revit task approaches the client timeout threshold (25 seconds), the governor intercepts. It sends a system-level response back to the LLM: *"Tool execution in progress. Host is processing complex geometry. Wait and do not retry."* The original task stays alive in the background, and its results are cached for the LLM's next poll.

3.  **Payload Auditing (Pre-validation):** 
    Strict validation rules block broad or dangerous queries (e.g., querying "Generic Models" with `include_geometry: true` without a filter) before they ever touch the Revit thread. This prevents "bad" requests from ever reaching the main thread's queue.

---

## 🧠 AnythingLLM Integration: The Intelligence Backbone

We have intentionally chosen **AnythingLLM** as our frontend and orchestration layer. This allows us to deliver a functional MVP without reinventing the wheel of local LLM management or vector storage. By leveraging AnythingLLM's existing infrastructure, we can focus 100% on the **Intent Engineering** of the Revit-Python bridge.

### 1. Configure the MCP Proxy
Instead of connecting AnythingLLM directly to the Autodesk `.exe`, we route it through our Python Proxy.

**Option A: Via AnythingLLM UI (Recommended)**
1. Open AnythingLLM and go to **Settings → Agent Configuration → MCP Servers**.
2. Click **Add New Server** and configure it as follows:
   - **Name:** `revit-2027`
   - **Type:** `command`
   - **Command:** Absolute path to your virtual environment's Python executable (e.g., `C:\Users\YourName\Documents\GitHub\revitmcp\venv\Scripts\python.exe`)
   - **Args:** Absolute path to `main_mcp.py` (e.g., `C:\Users\YourName\Documents\GitHub\revitmcp\main_mcp.py`)
3. Save and wait for AnythingLLM to discover the tools.

**Option B: Manual JSON Configuration**
1. Navigate to your AnythingLLM plugins folder (e.g., `%APPDATA%\AnythingLLMDesktop\storage\plugins` or equivalent).
2. Copy the `anythingllm_mcp_servers.json` file from the `anythingllm\mcp` directory of this repo into that directory.
3. **CRITICAL STEP:** Open the copied JSON file and edit the absolute paths. You **must** change `C:\path\to\your\revitmcp\...` to the actual location where you cloned this repository.
   - `command` should point to your `venv\Scripts\python.exe`
   - `args` should point to your `main_mcp.py`
4. Restart AnythingLLM. The new tools (`axo_audit_septic`, etc.) will appear natively in your agent's toolkit alongside the native Autodesk tools.

### 3. Configure the Workspace System Prompt

> [!IMPORTANT]
> This step is required for reliable results. Without it, LLM agents — especially smaller models — will omit critical query parameters and return empty results (e.g., `{ "levels": [] }`).

The Revit MCP tools require a **specific tool-call sequence** to return data correctly. Different LLMs infer this sequence inconsistently across new threads. A workspace system prompt enforces the correct behavior for every model, every time.

**How to apply:**
1. Open your AnythingLLM workspace.
2. Go to **Workspace Settings → Agent Configuration → System Prompt**.
3. Paste the prompt below and save. You do **not** need to restart the server.
4. Always start a **new thread** after updating the system prompt — existing threads won't inherit it.

**Copy this prompt exactly:**

```
You are a Revit BIM assistant with direct access to a live Revit 2027 model via the revit-2027 MCP tools.

### Recommended Workflow

The steps below describe a typical query flow. Adapt them to each request — if a tool is not available or returns unexpected results, proceed with what you have.

**Step 1 — Identify the active Revit instance**
Call `get_running_revit_instances` if available to discover the numeric `revitInstanceId`. If this tool is not in your toolkit, proceed without it — some models do not require an explicit instance ID.

**Step 2 — Query elements**
When calling `query_model`, include `"searchScope": "AllViews"` in every call. Without this parameter, the Revit API searches only the active view and returns empty results for most element types.

**Step 3 — Enrich with element details**
After `query_model` returns element IDs, call `get_element_data` with those IDs to retrieve names, parameters, and properties. Report meaningful data rather than raw element IDs.

### Valid query_model Parameters

Only these parameters are accepted. Do not add others:
- `revitInstanceId` (number) — if available from Step 1
- `input.categories` (array of strings, required) — e.g. `["OST_Levels"]`
- `input.searchScope` (string, required) — always `"AllViews"`
- `input.maxResults` (optional, number) — limit results
- `input.searchText` (optional, string) — filter by name

Parameters like `elementInclusionMode`, `includeTypes`, `scope`, or `outputOptions` are rejected by the MCP server.

### Rules

- **instanceId**: If `get_running_revit_instances` is available, use it. Hardcoded `revitInstanceId: 0` or `1` values may return empty results. If the tool is not available, query without it.
- **searchScope**: Always include `"searchScope": "AllViews"` in `query_model` calls. This is the most common source of empty results.
- **stop on error**: If `query_model` returns an error (not an empty list), stop. Do not retry with different parameters or categories. Report the error to the user.
- **empty results**: If a query returns an empty list, first verify the category name is correct and that `"searchScope": "AllViews"` is present before retrying.
- **category map**: Levels = `OST_Levels`, Walls = `OST_Walls`, Roofs = `OST_Roofs`, Rooms = `OST_Rooms`, Floors = `OST_Floors`, Property Lines = `OST_SiteProperty`, Areas = `OST_Areas`, Project Information = `OST_ProjectInformation`.
- **lot coverage**: When asked to calculate lot coverage, call `axo_audit_lot_coverage` in a single tool invocation. Do NOT attempt to calculate it manually by calling individual tools like `axo_audit_lot_area`, `query_model`, or `get_element_data` separately — the composite tool handles all queries internally and returns a deterministic result. Manual multi-step orchestration often fails due to overlapping floor areas, missing property line detection, and misinterpretation of intermediate results.
- **output format**: Present results in a readable, structured format — tables, bullet lists, or sections. Do not dump raw JSON.
- **tool invocations**: Use the native tool-calling interface provided by your platform. Do not write out simulated tool call syntax or JSON in your conversational responses.
```

**Why this is necessary:** The Autodesk Revit MCP Server defaults `searchScope` to the **active view** when the parameter is omitted. Since elements like Levels exist across all views, omitting `"AllViews"` causes the server to return an empty array even when the model is fully loaded. This is not a bug — it is expected API behavior that the agent must be explicitly instructed to handle.

---

### 4. Why AnythingLLM?
*   **Local RAG:** Instantly query BIM data against localized PDF building codes and zoning ordinances.
*   **Agentic Workflows:** Multi-step reasoning for complex compliance checks (e.g., "Find all walls, check their fire rating against the IBC documents in the vector store, and report failures").
*   **Privacy:** Keep all BIM data and firm-specific documents on your local network.

---

## ⚠️ Known Limitations & Bugs

### 1. Revit MCP Server Main Thread Deadlock

> [!CAUTION]
> The Autodesk Revit MCP Server relies on **single-threaded API access**. If an LLM agent issues multiple rapid-fire `query_model` calls that fail (e.g., due to invalid parameters or timeouts), the server's internal query processing thread can become permanently blocked — even though the pipe transport layer remains alive.

**Symptoms:**
* `tools/list` responds instantly, but all `query_model` calls hang indefinitely — including categories that previously worked (e.g., `OST_Levels`).
* AnythingLLM shows "Agent complete" with no results, or the thread appears stuck and eventually times out.

**Workaround:** Restart the Autodesk Revit MCP Server plugin or restart Revit entirely. There is currently no way to clear the deadlock without a restart.

**Root cause:** When an LLM agent retries a failing tool call in a tight loop (common in AnythingLLM's agent executor), the queued JSON-RPC requests pile up on the pipe. The MCP server's main thread gets blocked processing a failed request and never releases, starving all subsequent queries.

**Mitigation:** The **Governance Layer** (see section above) significantly reduces the risk of this deadlock by intercepting and coalescing duplicate retries and providing heartbeat keep-alives. However, a hard freeze can still occur if the Revit API itself hits an unrecoverable state during a geometry-heavy operation.

### 2. AnythingLLM Agent Retry Behavior Cannot Be Fully Controlled via System Prompt

AnythingLLM's agent executor has **built-in retry logic** at the platform level. Even if the workspace system prompt instructs the model to "stop immediately on error," the agent loop may still re-invoke the tool. This is an AnythingLLM platform limitation — the system prompt controls the LLM's reasoning but not the executor's retry policy. This retry behavior is the primary trigger for the main thread deadlock described above.

### 3. `OST_Roofs` Queries May Fail on Complex Models

Querying `OST_Roofs` via `query_model` has been observed to fail or time out on models with complex roof geometry, even when other categories (`OST_Levels`, `OST_Walls`) return successfully. This may be a limitation of the Autodesk MCP Server's Technical Preview. If roof queries consistently fail, try reducing `maxResults` to `1` to isolate whether the category is supported for the current model.

### 4. Inherent Latency and Governance Heartbeats

Because Revit processes all database queries sequentially on a single main thread, complex queries (like extracting parameter data for hundreds of elements) inherently take time. If multiple requests are sent, they are forced to queue up and wait their turn.

The Governance Layer does not *create* this latency, but it actively manages it. If a Revit task approaches the client timeout threshold, the governor intervenes by sending a heartbeat back to AnythingLLM to keep the connection alive. From the user's perspective, this means you may experience noticeable delays (sometimes 30-60+ seconds) while waiting for an agent to finish a complex task. This is expected behavior and a direct result of Revit's single-threaded architecture queuing the workload.

### 5. Read-Only and Host-Model Access Only

The Revit MCP is currently restricted by the following environmental constraints:

*   **Read-Only Access:** The engine can only query and retrieve data from the model.
*   **Host Model Only:** Only the elements within the primary host model are available for query.
*   **Linked Models Inaccessible:** Data from linked Revit models in the primary host model is currently not accessible, similar to limitations found in the Revit 2027 built-in Autodesk Assistant

---

## ⚖️ Legal Disclaimer & Liability

**Important Notice:** The Axoworks Logic Engine is an experimental, AI-powered QA/QC reference tool. 

* **Not Professional Advice:** This software does not constitute professional architectural, engineering, or legal advice. It is not a substitute for the judgment of a licensed Architect or Engineer of Record.
* **No Guarantee of Compliance:** While the deterministic engine is designed to accurately apply strict mathematical formulas to Revit data, building codes are subject to localized interpretations, exceptions, and updates. The results of any `/audit` (e.g., septic setbacks, energy envelopes, WWR) must be independently verified by a qualified professional.
* **Use at Your Own Risk:** By using this software, you agree that the authors, Axoworks, and contributors are not liable for any code violations, construction defects, damages, or financial losses resulting from the use of (or reliance upon) this software.

See the `LICENSE` file for full warranty and liability details.
