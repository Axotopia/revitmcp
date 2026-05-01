# Workspace System Prompt Revision

## Test Findings Summary

| Behavior | Original Prompt | My Initial Proposal |
|---|---|---|
| Skipped Step 1 when tool missing | ✅ Proceeded pragmatically | ❌ Halted completely |
| Used `searchScope: "AllViews"` | ✅ Yes | N/A (halted) |
| Used `get_element_data` for enrichment | ✅ Yes | N/A (halted) |
| Injected invalid params (`outputOptions`) | ❌ Yes | N/A (halted) |
| Got final result | ✅ "463 Glory View Ln, Manson, WA" | ❌ No result |

**Key Insight:** The original aggressive formatting paradoxically produced *more* pragmatic behavior. Qwen discounted the ALL CAPS noise and adapted. My cleaner version made the rules feel more real, causing rigid compliance and task failure.

## Design Principles for the Revision

1. **Built-in pragmatism** — Explicitly tell the model it can adapt when tools aren't available
2. **Sequence as guidance** — Frame steps as a recommended workflow, not hard requirements
3. **Strong parameter validation** — Prevent invalid params like `outputOptions`
4. **Keep what worked** — Category mappings, `searchScope: "AllViews"`, `get_element_data` enrichment
5. **Minimal emphasis** — No ALL CAPS, no `**CRITICAL:**`, bold only for section headers
6. **Escape hatch** — "If a tool isn't available, adapt and proceed" instead of halting

---

## Revised System Prompt

````
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
- **category map**: Levels = `OST_Levels`, Walls = `OST_Walls`, Roofs = `OST_Roofs`, Rooms = `OST_Rooms`, Project Information = `OST_ProjectInformation`.
- **output format**: Present results in a readable, structured format — tables, bullet lists, or sections. Do not dump raw JSON.
- **tool invocations**: Use the native tool-calling interface provided by your platform. Do not write out simulated tool call syntax or JSON in your conversational responses.
````

---

## What Changed vs. Original

| Section | Original | Revised | Rationale |
|---|---|---|---|
| Header | `## REQUIRED TOOL CALL SEQUENCE` | `### Recommended Workflow` | Removed ALL CAPS, signaled flexibility |
| Step 1 | "Call `get_running_revit_instances` before any other tool." | "Call `get_running_revit_instances` if available... If this tool is not in your toolkit, proceed without it." | Added escape hatch — prevents halting |
| Step 2 | "You MUST include..." | "include `searchScope`: `AllViews` in every call" | Softer language, equally clear |
| Parameters | "Do NOT add..." / "Never add any others" | "Do not add others" + explicit `outputOptions` mention | Added `outputOptions` to the forbidden list (test showed this injection) |
| RULES | "Never skip Step 1" | "If the tool is not available, query without it" | Positive framing with pragmatism |
| RULES | — (missing) | **empty results**: "verify category name is correct" | Added category verification alongside scope check |
| RULES | — (missing) | **category map**: added `OST_ProjectInformation` | Test showed this is a commonly needed category |
| RULES | — (missing) | **tool invocations**: "Use the native tool-calling interface" | Clearer than "hidden JSON function-calling schema" |
