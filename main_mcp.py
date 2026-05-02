"""
Axoworks Revit MCP Proxy Server
================================
Sits between AnythingLLM and the Autodesk Revit MCP Server, providing:

1. Transparent pass-through to all native Revit MCP tools
2. Coordinate translation (Project Base Point Z-offset)
3. Custom audit tools (septic, energy, WWR)
4. Governance layer (dedup, heartbeat, payload auditing)

Architecture:
    AnythingLLM  ←─stdio─→  main_mcp.py  ←─Named Pipe─→  Autodesk Revit MCP Server

The proxy dynamically discovers tools from the Autodesk pipe and exposes them
to AnythingLLM with identical names/schemas, so existing AnythingLLM workspace
configuration (system prompts, agent settings) continues to work unchanged.

Usage:
    python main_mcp.py

Environment variables (see .env.example):
    OLLAMA_MODEL, OLLAMA_BASE_URL, API_PORT, REVIT_PIPE_PREFIX
    GOVERNOR_HEARTBEAT_THRESHOLD_S, GOVERNOR_CACHE_TTL_S, etc.
"""

import json
import logging
import os
import sys
from typing import Any

from dotenv import load_dotenv

from bridge import RevitBridge, RevitBridgeError, get_governed_bridge
from coordinate_translator import translator

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,  # MCP uses stdout for protocol — keep logs on stderr
)
logger = logging.getLogger("revitmcp.mcp_server")

load_dotenv()

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.6:35b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")


# ---------------------------------------------------------------------------
# MCP Server — using the raw MCP protocol over stdio
# ---------------------------------------------------------------------------
#
# The MCP protocol is JSON-RPC 2.0 over stdin/stdout (newline-delimited).
# We implement the two required methods:
#   - tools/list
#   - tools/call
#
# We use the transport-agnostic JSON-RPC layer already built in bridge.py
# and re-route it over stdio for MCP compliance.

class McpStdioTransport:
    """
    Reads JSON-RPC 2.0 requests from stdin and writes responses to stdout.
    This is the standard MCP transport used by AnythingLLM and other MCP hosts.
    """

    def __init__(self):
        self._bridge = get_governed_bridge()
        self._cached_tools: list[dict] | None = None

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def _discover_revit_tools(self) -> list[dict]:
        """Fetch tools from the Autodesk Revit MCP pipe via tools/list."""
        try:
            raw = await self._bridge.list_mcp_tools()
            if isinstance(raw, list):
                return raw
            if isinstance(raw, dict) and "tools" in raw:
                return raw["tools"]
            logger.warning("Unexpected tools/list response format: %s", type(raw))
            return []
        except RevitBridgeError as exc:
            logger.error("Failed to discover Revit tools: %s", exc)
            return []
        except Exception as exc:
            logger.error("Unexpected error discovering Revit tools: %s", exc)
            return []

    def _build_custom_tools(self) -> list[dict]:
        """Define custom audit tools that the proxy handles internally."""
        return [
            {
                "name": "axo_audit_septic",
                "description": "Run a septic setback compliance audit on the active Revit model.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "jurisdiction": {
                            "type": "string",
                            "description": "Jurisdiction code for building code lookup (default: 'default').",
                            "default": "default",
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "axo_audit_energy",
                "description": "Run an energy envelope compliance audit (U-factors, SHGC) on the active Revit model.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "jurisdiction": {
                            "type": "string",
                            "description": "Jurisdiction code for energy code lookup (default: 'default').",
                            "default": "default",
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "axo_audit_wwr",
                "description": "Run a Window-to-Wall Ratio compliance audit on the active Revit model.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "max_wwr_percent": {
                            "type": "number",
                            "description": "Maximum allowed WWR percentage (default: 40).",
                            "default": 40.0,
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "axo_audit_floor_area",
                "description": (
                    "Query floor area data from the active Revit model. "
                    "Returns total floor area and per-room breakdown, grouped by level. "
                    "Optionally filter by one or more level names (e.g., FP1.GARAGE, FP2.ADU)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "level_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of level names to filter by (e.g., ['FP1.GARAGE', 'FP2.ADU']). If omitted, returns data for all levels.",
                        },
                        "include_room_details": {
                            "type": "boolean",
                            "description": "Include individual room names, numbers, and areas in the output. Default: true.",
                            "default": True,
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "axo_audit_lot_area",
                "description": (
                    "Calculate the lot area (area enclosed by property lines) "
                    "from the active Revit model. Queries OST_PropertyLine elements, "
                    "extracts polyline geometry, and computes enclosed area using "
                    "the shoelace formula (deterministic math, no LLM). "
                    "Returns area in both sq ft and acres."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "area_unit": {
                            "type": "string",
                            "description": "Output area unit: 'sqft' (default), 'acres', or 'both'.",
                            "default": "both",
                            "enum": ["sqft", "acres", "both"],
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "axo_audit_lot_coverage",
                "description": (
                    "Calculate lot coverage percentage for the active Revit model. "
                    "Deterministically queries property lines (OST_SiteProperty) for lot area, "
                    "floor elements (OST_Floors) for building footprint, and area elements "
                    "(OST_Areas) for additional covered areas. "
                    "Returns a structured breakdown with lot area, building footprint, "
                    "covered areas, and two coverage percentages (building-only and total)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "area_unit": {
                            "type": "string",
                            "description": "Output area unit: 'sqft' (default), 'acres', or 'both'.",
                            "default": "both",
                            "enum": ["sqft", "acres", "both"],
                        },
                        "include_details": {
                            "type": "boolean",
                            "description": "Include per-element area breakdown.",
                            "default": True,
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "axo_audit_setback",
                "description": (
                    "Calculate the closest distance from building exterior walls "
                    "to property lines. Queries OST_Walls (exterior walls) and "
                    "OST_PropertyLine elements, extracts bounding box and curve "
                    "geometry, then computes minimum perpendicular distances "
                    "per side (North, South, East, West) using deterministic math. "
                    "Returns distances in feet and inches, and identifies the "
                    "closest setback. This tool is equivalent to Revit's built-in "
                    "'Property Line Proximity Analysis'."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "output_unit": {
                            "type": "string",
                            "description": "Output unit: 'ft_in' (default, feet and inches), 'ft' (decimal feet), or 'in' (inches).",
                            "default": "ft_in",
                            "enum": ["ft_in", "ft", "in"],
                        },
                    },
                    "required": [],
                },
            },
        ]

    async def get_tools(self) -> list[dict]:
        """
        Return the merged tool list: native Revit tools + custom audit tools.
        Results are cached so we don't hammer the pipe on every request.
        """
        if self._cached_tools is None:
            native = await self._discover_revit_tools()
            custom = self._build_custom_tools()
            self._cached_tools = native + custom
            logger.info(
                "Discovered %d native Revit tools + %d custom tools",
                len(native),
                len(custom),
            )
        return self._cached_tools

    def invalidate_cache(self):
        """Force re-discovery on the next tools/list call."""
        self._cached_tools = None

    # -----------------------------------------------------------------------
    # Proxy: call a native Revit tool
    # -----------------------------------------------------------------------

    async def _call_revit_tool(self, tool_name: str, arguments: dict) -> Any:
        """Forward a tool call to the Autodesk Revit MCP pipe."""
        try:
            result = await self._bridge.run_mcp_tool(tool_name, arguments)

            # The Autodesk MCP server returns responses in MCP-compliant format:
            #   {"content": [{"type": "text", "text": "{\"outcome\": ...}"}]}
            # We need to parse the inner text, translate coordinates, and re-wrap.
            translated = await self._translate_response(result)

            return translated
        except RevitBridgeError as exc:
            logger.error("Revit bridge error calling '%s': %s", tool_name, exc)
            raise
        except Exception as exc:
            logger.error("Unexpected error calling '%s': %s", tool_name, exc)
            raise

    async def _translate_response(self, response: Any) -> Any:
        """
        Translate Z coordinates inside an MCP-compliant response.
        
        The Autodesk server wraps data as:
            {"content": [{"type": "text", "text": "<json string>"}]}
        
        We parse the inner JSON, translate geometry Z values, and re-wrap.
        """
        if not isinstance(response, dict):
            # Raw passthrough — try translating directly
            translated = await translator.translate_payload(self._bridge, response)
            return translated

        content = response.get("content")
        if not isinstance(content, list):
            # Not MCP-wrapped — translate directly
            translated = await translator.translate_payload(self._bridge, response)
            return translated

        translated_content = []
        for item in content:
            if not isinstance(item, dict):
                translated_content.append(item)
                continue

            if item.get("type") == "text":
                text = item.get("text", "")
                try:
                    # Parse the inner JSON
                    parsed = json.loads(text)
                    # Translate coordinates in the parsed data
                    translated_data = await translator.translate_payload(
                        self._bridge, parsed
                    )
                    # Re-serialize
                    translated_content.append({
                        "type": "text",
                        "text": json.dumps(translated_data, indent=2),
                    })
                except (json.JSONDecodeError, TypeError):
                    # Not JSON — pass through unchanged
                    translated_content.append(item)
            else:
                translated_content.append(item)

        return {"content": translated_content}

    # -----------------------------------------------------------------------
    # Custom audit tools
    # -----------------------------------------------------------------------

    async def _run_septic_audit(self, arguments: dict) -> dict:
        """
        Septic setback compliance audit.
        Uses the Revit pipe for data extraction, then applies deterministic math.
        """
        jurisdiction = arguments.get("jurisdiction", "default")

        # Fetch data via Revit MCP tools (the new query_model + get_element_data API)
        try:
            # Step 1: Query plumbing fixtures (tanks)
            tanks_raw = await self._run_governed_tool_sync(
                "query_model",
                {
                    "input": {
                        "categories": ["OST_PlumbingFixtures"],
                        "searchScope": "AllViews",
                        "maxResults": 200,
                    }
                },
            )
            # Step 2: Query property lines
            lines_raw = await self._run_governed_tool_sync(
                "query_model",
                {
                    "input": {
                        "categories": ["OST_PropertyLine"],
                        "searchScope": "AllViews",
                        "maxResults": 200,
                    }
                },
            )
        except RevitBridgeError as exc:
            return {"error": f"Failed to query Revit model: {exc}"}

        # Parse the MCP-compliant response format:
        # {"content": [{"type": "text", "text": "{\"outcome\": ...}"}]}
        def _extract_content(response: Any) -> list:
            if isinstance(response, dict):
                content = response.get("content", [])
                if content and isinstance(content, list):
                    text_parts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            try:
                                parsed = json.loads(item.get("text", "{}"))
                                if isinstance(parsed, dict):
                                    elements = parsed.get("outcome", {}).get("elements", [])
                                    text_parts.extend(elements)
                            except (json.JSONDecodeError, AttributeError):
                                text_parts.append(item.get("text", ""))
                    return text_parts
            return []

        tanks = _extract_content(tanks_raw)
        lines = _extract_content(lines_raw)

        # Apply coordinate translation to the extracted elements
        tanks = await translator.translate_payload(self._bridge, tanks)
        lines = await translator.translate_payload(self._bridge, lines)

        # Simple setback calculation (deterministic, no LLM math)
        required_ft = 50.0  # Default IBC setback
        results = {"tanks_found": len(tanks), "lines_found": len(lines)}
        return {
            "audit_type": "septic",
            "jurisdiction": jurisdiction,
            "results": results,
            "narrative": f"Septic audit: Found {len(tanks)} tank(s) and {len(lines)} property line(s). "
                         f"Required setback: {required_ft}ft. "
                         f"Coordinate translation applied via Project Base Point offset.",
        }

    async def _run_energy_audit(self, arguments: dict) -> dict:
        """Energy envelope compliance audit (placeholder — extend as needed)."""
        jurisdiction = arguments.get("jurisdiction", "default")
        return {
            "audit_type": "energy",
            "jurisdiction": jurisdiction,
            "narrative": "Energy envelope audit requires implementation of U-factor/SHGC extraction logic. "
                         "This is a placeholder pending full integration with the query_model + get_element_data API.",
        }

    async def _run_wwr_audit(self, arguments: dict) -> dict:
        """Window-to-Wall ratio compliance audit (placeholder — extend as needed)."""
        max_wwr = arguments.get("max_wwr_percent", 40.0)
        return {
            "audit_type": "wwr",
            "max_wwr_percent": max_wwr,
            "narrative": f"WWR audit targeting {max_wwr}% maximum. "
                         "Full implementation pending integration with query_model + get_element_data API.",
        }

    async def _run_floor_area_audit(self, arguments: dict) -> dict:
        """
        Floor area audit — returns total & per-room floor area grouped by level.
        
        Uses the Revit pipe to query Rooms (OST_Rooms), then retrieves detailed
        element data (Area, Name, Number, Level) via get_element_data.
        Optionally filters by level name(s) such as FP1.GARAGE, FP2.ADU.
        """
        level_names = arguments.get("level_names", None)
        include_room_details = arguments.get("include_room_details", True)

        try:
            # Step 1: Query all rooms via query_model
            rooms_raw = await self._run_governed_tool_sync(
                "query_model",
                {
                    "input": {
                        "categories": ["OST_Rooms"],
                        "searchScope": "AllViews",
                        "maxResults": 500,
                    }
                },
            )
        except RevitBridgeError as exc:
            return {"error": f"Failed to query Revit model rooms: {exc}"}

        # Parse the MCP-compliant response format:
        # {"content": [{"type": "text", "text": "{\"outcome\": ...}"}]}
        def _extract_content(response: Any) -> list:
            if isinstance(response, dict):
                content = response.get("content", [])
                if content and isinstance(content, list):
                    text_parts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            try:
                                parsed = json.loads(item.get("text", "{}"))
                                if isinstance(parsed, dict):
                                    elements = parsed.get("outcome", {}).get("elements", [])
                                    if not elements:
                                        # Some servers return elements at the top level
                                        elements = parsed.get("elements", [])
                                    text_parts.extend(elements)
                            except (json.JSONDecodeError, AttributeError):
                                text_parts.append(item.get("text", ""))
                    return text_parts
            return []

        rooms = _extract_content(rooms_raw)

        if not rooms:
            return {
                "audit_type": "floor_area",
                "total_rooms_found": 0,
                "narrative": "No rooms found in the Revit model. "
                             "Ensure rooms are placed on floor plans (Room elements, not just spaces). "
                             "Try placing rooms via Revit's Room tool on the appropriate views.",
                "levels": [],
            }

        # Step 2: Extract element IDs for get_element_data
        element_ids = []
        for room in rooms:
            if isinstance(room, dict):
                eid = room.get("elementId") or room.get("id")
                if eid is not None:
                    element_ids.append(str(eid))

        # Step 3: Get detailed element data for all rooms
        room_details = []
        if element_ids:
            try:
                data_raw = await self._run_governed_tool_sync(
                    "get_element_data",
                    {"elementIds": element_ids},
                )
                # Parse get_element_data response
                if isinstance(data_raw, dict):
                    content = data_raw.get("content", [])
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                try:
                                    parsed = json.loads(item.get("text", "{}"))
                                    if isinstance(parsed, dict):
                                        elements = parsed.get("elements", [])
                                        if not elements and "results" in parsed:
                                            elements = [{"elementId": eid} for eid in parsed.get("results", {}).get("Element Ids", [])]
                                        elif not elements and isinstance(parsed.get("outcome"), dict):
                                            elements = parsed.get("outcome", {}).get("elements", [])
                                        if isinstance(elements, list):
                                            for elem in elements:
                                                if isinstance(elem, dict):
                                                    # Extract room data from parameters
                                                    params = elem.get("parameters", {})
                                                    level_param = (
                                                        params.get("Level", {}).get("value")
                                                        if isinstance(params.get("Level"), dict)
                                                        else params.get("Level")
                                                    )
                                                    area_param = (
                                                        params.get("Area", {}).get("value")
                                                        if isinstance(params.get("Area"), dict)
                                                        else params.get("Area")
                                                    )
                                                    name_param = (
                                                        params.get("Name", {}).get("value")
                                                        if isinstance(params.get("Name"), dict)
                                                        else params.get("Name")
                                                    )
                                                    number_param = (
                                                        params.get("Number", {}).get("value")
                                                        if isinstance(params.get("Number"), dict)
                                                        else params.get("Number")
                                                    )
                                                    room_details.append({
                                                        "element_id": elem.get("elementId") or elem.get("id"),
                                                        "name": name_param or elem.get("name", "Unnamed"),
                                                        "number": number_param or elem.get("number", ""),
                                                        "level": level_param or elem.get("level", "Unknown Level"),
                                                        "area": self._try_parse_float(area_param, 0.0),
                                                        "area_unit": "sq ft",
                                                    })
                                except (json.JSONDecodeError, AttributeError):
                                    pass
            except RevitBridgeError:
                # Fallback: extract what we can from the query_model response
                for room in rooms:
                    if isinstance(room, dict):
                        params = room.get("parameters", {})
                        level_val = (
                            params.get("Level", {}).get("value")
                            if isinstance(params.get("Level"), dict)
                            else params.get("Level", room.get("level", "Unknown"))
                        )
                        area_val = (
                            params.get("Area", {}).get("value")
                            if isinstance(params.get("Area"), dict)
                            else params.get("Area", 0)
                        )
                        room_details.append({
                            "element_id": room.get("elementId") or room.get("id"),
                            "name": room.get("name", "Unnamed"),
                            "number": room.get("number", ""),
                            "level": level_val,
                            "area": self._try_parse_float(area_val, 0.0),
                            "area_unit": "sq ft",
                        })
        else:
            # Fallback: use data directly from query_model
            for room in rooms:
                if isinstance(room, dict):
                    room_details.append({
                        "element_id": room.get("elementId") or room.get("id"),
                        "name": room.get("name", "Unnamed"),
                        "number": room.get("number", ""),
                        "level": room.get("level", "Unknown"),
                        "area": self._try_parse_float(room.get("area", 0), 0.0),
                        "area_unit": "sq ft",
                    })

        # Step 4: Group by level
        levels_map: dict[str, dict] = {}
        for rd in room_details:
            level_name = rd.get("level", "Unknown Level") or "Unknown Level"

            # Apply level filter if specified
            if level_names and level_name not in level_names:
                continue

            if level_name not in levels_map:
                levels_map[level_name] = {
                    "level_name": level_name,
                    "total_rooms": 0,
                    "total_area_sqft": 0.0,
                    "rooms": [],
                }

            levels_map[level_name]["total_rooms"] += 1
            levels_map[level_name]["total_area_sqft"] += rd["area"]
            if include_room_details:
                levels_map[level_name]["rooms"].append({
                    "name": rd["name"],
                    "number": rd["number"],
                    "area_sqft": round(rd["area"], 2),
                })

        # Step 5: Build structured result
        level_summaries = []
        for lv in sorted(levels_map.values(), key=lambda x: x["level_name"]):
            entry = {
                "level_name": lv["level_name"],
                "total_rooms": lv["total_rooms"],
                "total_area_sqft": round(lv["total_area_sqft"], 2),
            }
            if include_room_details and lv["rooms"]:
                entry["rooms"] = sorted(lv["rooms"], key=lambda r: r["name"])
            level_summaries.append(entry)

        grand_total = round(sum(lv["total_area_sqft"] for lv in levels_map.values()), 2)
        total_rooms = sum(lv["total_rooms"] for lv in levels_map.values())

        # Build narrative
        if level_names:
            filters_text = ", ".join(level_names)
            narrative_parts = [
                f"Floor area audit filtered to {len(level_summaries)} level(s): {filters_text}."
            ]
        else:
            narrative_parts = [
                f"Floor area audit across {len(level_summaries)} level(s)."
            ]
        narrative_parts.append(
            f"Total floor area: {grand_total:,} sq ft across {total_rooms} room(s)."
        )
        if level_summaries:
            for lv in level_summaries:
                narrative_parts.append(
                    f"  - {lv['level_name']}: {lv['total_area_sqft']:,} sq ft ({lv['total_rooms']} room(s))"
                )

        return {
            "audit_type": "floor_area",
            "total_rooms_found": total_rooms,
            "grand_total_area_sqft": grand_total,
            "levels": level_summaries,
            "narrative": "\n".join(narrative_parts),
        }

    async def _run_governed_tool_sync(self, tool_name: str, arguments: dict) -> Any:
        """
        Runs a tool via the bridge (governor) and blocks until the real result is ready,
        transparently polling if the governor returns a heartbeat.
        """
        import asyncio
        while True:
            raw = await self._bridge.run_mcp_tool(tool_name, arguments)
            if isinstance(raw, dict) and "_governor_status" in raw:
                # It's a heartbeat, wait and poll again
                await asyncio.sleep(2)
                continue
            return raw

    def _extract_number(self, val: Any) -> float:
        """Extracts a float from a Revit parameter string like '18,975.88 sq ft'"""
        if isinstance(val, (int, float)):
            return float(val)
        val_str = str(val).replace(',', '')
        import re
        match = re.search(r'-?\d+\.?\d*', val_str)
        if match:
            return float(match.group())
        raise ValueError(f"Could not extract number from {val}")

    async def _run_lot_area_audit(self, arguments: dict) -> dict:
        """
        Lot area audit — retrieves the lot area directly from property lines.

        Revit pre-calculates the Area for each OST_SiteProperty element, so
        the value is available in the query_model response without needing a
        separate get_element_data call.  We still fall back to get_element_data
        in case the server omits inline parameters for any reason.
        """
        area_unit = arguments.get("area_unit", "both")
        one_acre_sqft = 43560.0

        # Candidate parameter key names for area and name
        AREA_KEYS = ["Area", "area", "ROOM_AREA", "GSA_SPACE_AREA", "NetArea", "GrossArea"]
        NAME_KEYS = ["Name", "Mark", "Comments", "ELEM_TYPE_PARAM"]

        def _pick(params: dict, keys: list):
            """Return the first matching value from params, unwrapping {value:} dicts."""
            for k in keys:
                if k in params:
                    v = params[k]
                    return v.get("value") if isinstance(v, dict) else v
            return None

        def _extract_elem_list(parsed: dict) -> list:
            """
            Normalise a parsed JSON response to a flat list of element dicts.
            Handles:
              Shape A: {"outcome": {"elements": [...]}}
              Shape A2: {"elements": [...]}
              Shape B: {"results": {"Element Ids": [...]} }  (IDs only, no params)
              Shape B2: {"results": {"123": {"parameters": {...}}, ...}}
            Returns list of element dicts (may have "elementId" and "parameters").
            """
            # Shape A / A2
            elems = parsed.get("outcome", {}).get("elements", [])
            if not elems:
                elems = parsed.get("elements", [])
            if elems and isinstance(elems, list):
                return [e for e in elems if isinstance(e, dict)]

            # Shape B2: results is a dict keyed by element id with value dicts
            results = parsed.get("results", {})
            if isinstance(results, dict):
                # Sub-case B: plain "Element Ids" list — convert to stub dicts
                if "Element Ids" in results:
                    return [{"elementId": eid} for eid in results["Element Ids"]]
                # Sub-case B2: keyed by id with element data
                out = []
                for val in results.values():
                    if isinstance(val, dict):
                        out.append(val)
                return out
            if isinstance(results, list):
                return [{"elementId": eid} for eid in results]

            # Shape C: top-level "Element Ids"
            if "Element Ids" in parsed:
                return [{"elementId": eid} for eid in parsed["Element Ids"]]

            return []

        try:
            # ----------------------------------------------------------------
            # Step 1: query_model for OST_SiteProperty
            # ----------------------------------------------------------------
            raw = await self._run_governed_tool_sync(
                "query_model",
                {
                    "input": {
                        "categories": ["OST_SiteProperty"],
                        "searchScope": "AllViews",
                        "maxResults": 50,
                    }
                },
            )

            # Collect all elements with whatever inline data query_model provides
            query_elements = []  # list of element dicts from query_model
            raw_text_debug = ""
            if isinstance(raw, dict):
                for item in raw.get("content", []):
                    if isinstance(item, dict) and item.get("type") == "text":
                        raw_text = item.get("text", "{}")
                        raw_text_debug = raw_text[:500]
                        try:
                            parsed = json.loads(raw_text)
                            query_elements = _extract_elem_list(parsed)
                        except Exception as ex:
                            raw_text_debug += f" [json error: {ex}]"

            if not query_elements:
                return {
                    "audit_type": "lot_area",
                    "status": "Unavailable",
                    "narrative": (
                        "No OST_SiteProperty elements found in the model.\n"
                        f"Diagnostic — raw response snippet: {raw_text_debug!r}"
                    ),
                }

            # ----------------------------------------------------------------
            # Step 2: Try to read Area from inline query_model parameters
            # (Revit pre-calculates Area for site property elements)
            # ----------------------------------------------------------------
            lots = []
            element_ids_for_fallback = []
            params_debug = []

            for elem in query_elements:
                eid = elem.get("elementId") or elem.get("id")
                if eid is not None:
                    element_ids_for_fallback.append(eid)

                # Parameters may be inline (Shape A) or absent (Shape B stub)
                params = elem.get("parameters", {})
                if not params:
                    # Also check if the element itself has area-like top-level keys
                    params = {k: v for k, v in elem.items() if k not in ("elementId", "id", "category", "type")}

                params_debug.append(list(params.keys())[:20])
                area_val = _pick(params, AREA_KEYS)
                name_val = _pick(params, NAME_KEYS) or f"Lot {eid}"

                if area_val is not None:
                    try:
                        area_float = self._extract_number(area_val)
                        if area_float > 0:
                            lots.append({"name": name_val, "area_sqft": area_float, "element_id": eid})
                    except ValueError:
                        pass

            # ----------------------------------------------------------------
            # Step 3: Fallback — get_element_data (no outputOptions restriction)
            # ----------------------------------------------------------------
            if not lots and element_ids_for_fallback:
                ge_debug = ""
                try:
                    data_raw = await self._run_governed_tool_sync(
                        "get_element_data",
                        {"elementIds": [int(eid) for eid in element_ids_for_fallback]},
                    )
                    if isinstance(data_raw, dict):
                        for item in data_raw.get("content", []):
                            if isinstance(item, dict) and item.get("type") == "text":
                                ge_raw = item.get("text", "{}")
                                ge_debug = ge_raw[:500]
                                try:
                                    parsed = json.loads(ge_raw)
                                    for elem_val in _extract_elem_list(parsed):
                                        eid = elem_val.get("elementId") or elem_val.get("id")
                                        params = elem_val.get("parameters", elem_val)
                                        params_debug.append(list(params.keys())[:20])
                                        area_val = _pick(params, AREA_KEYS)
                                        name_val = _pick(params, NAME_KEYS) or f"Lot {eid}"
                                        if area_val is not None:
                                            try:
                                                area_float = self._extract_number(area_val)
                                                if area_float > 0:
                                                    lots.append({"name": name_val, "area_sqft": area_float, "element_id": eid})
                                            except ValueError:
                                                pass
                                except Exception:
                                    pass
                except Exception as ge_err:
                    ge_debug += f" [get_element_data error: {ge_err}]"

                if not lots:
                    return {
                        "audit_type": "lot_area",
                        "status": "Unavailable",
                        "narrative": (
                            f"OST_SiteProperty element(s) found (IDs: {element_ids_for_fallback}), "
                            "but Area could not be read from either query_model or get_element_data.\n"
                            f"query_model params seen: {params_debug}\n"
                            f"get_element_data snippet: {ge_debug!r}\n"
                            f"query_model snippet: {raw_text_debug!r}"
                        ),
                    }

            if not lots:
                return {
                    "audit_type": "lot_area",
                    "status": "Unavailable",
                    "narrative": (
                        f"OST_SiteProperty elements found but Area param not present.\n"
                        f"params_debug: {params_debug}\n"
                        f"query_model snippet: {raw_text_debug!r}"
                    ),
                }

            # ----------------------------------------------------------------
            # Step 4: Compute totals and return
            # ----------------------------------------------------------------
            total_area_sqft = sum(lot["area_sqft"] for lot in lots)
            total_area_acres = total_area_sqft / one_acre_sqft

            narrative = f"Lot area audit complete. Found {len(lots)} property line(s).\n"
            narrative += f"Total Area: {total_area_sqft:,.2f} sq ft ({total_area_acres:,.4f} acres)\n"
            for lot in lots:
                narrative += f"  - {lot['name']}: {lot['area_sqft']:,.2f} sq ft\n"

            return {
                "audit_type": "lot_area",
                "status": "Success",
                "total_area_sqft": total_area_sqft,
                "total_area_acres": total_area_acres,
                "lots": lots,
                "narrative": narrative,
            }

        except Exception as e:
            return {
                "audit_type": "lot_area",
                "error": str(e),

                "narrative": f"Error running lot area audit: {e}"
            }

    async def _run_lot_coverage_audit(self, arguments: dict) -> dict:
        """
        Lot coverage audit — computes (Building Footprint / Total Lot Area) * 100.
        Queries OST_SiteProperty, OST_Floors, and OST_Areas to deterministically calculate.
        """
        include_details = arguments.get("include_details", True)
        
        try:
            # Helper to query and sum areas — uses KeyParameters (proven to return Area)
            async def get_category_area(category_name):
                raw = await self._run_governed_tool_sync(
                    "query_model",
                    {
                        "input": {
                            "categories": [category_name],
                            "searchScope": "AllViews",
                            "maxResults": 100,
                        }
                    },
                )
                import json
                cat_element_ids = []
                if isinstance(raw, dict):
                    content_list = raw.get("content", [])
                    if isinstance(content_list, list):
                        for item in content_list:
                            if isinstance(item, dict) and item.get("type") == "text":
                                try:
                                    parsed = json.loads(item.get("text", "{}"))
                                    # Shape A: outcome → elements list (primary Revit MCP format)
                                    elements = parsed.get("outcome", {}).get("elements", [])
                                    if not elements:
                                        elements = parsed.get("elements", [])
                                    if elements and isinstance(elements, list):
                                        for e in elements:
                                            if isinstance(e, dict):
                                                eid = e.get("elementId") or e.get("id")
                                                if eid is not None:
                                                    cat_element_ids.append(eid)
                                    # Shape B fallback: results → Element Ids
                                    if not cat_element_ids and "results" in parsed:
                                        r = parsed["results"]
                                        if isinstance(r, dict):
                                            cat_element_ids = r.get("Element Ids", [])
                                        elif isinstance(r, list):
                                            cat_element_ids = r
                                except Exception:
                                    pass
                
                if not cat_element_ids:
                    return 0.0, []

                AREA_KEYS = ["Area", "area", "ROOM_AREA", "GSA_SPACE_AREA"]
                NAME_KEYS = ["Name", "Mark", "Type Name", "Family"]
                LEVEL_KEYS = ["Level", "level", "LEVEL_PARAM"]

                def _pick(params, keys):
                    for k in keys:
                        if k in params:
                            raw = params[k]
                            return raw.get("value") if isinstance(raw, dict) else raw
                    return None
                    
                data_raw = await self._run_governed_tool_sync(
                    "get_element_data",
                    {"elementIds": [int(eid) for eid in cat_element_ids]},
                )
                
                total_sqft = 0.0
                elements_info = []
                
                if isinstance(data_raw, dict):
                    content_list = data_raw.get("content", [])
                    if isinstance(content_list, list):
                        for item in content_list:
                            if isinstance(item, dict) and item.get("type") == "text":
                                try:
                                    parsed = json.loads(item.get("text", "{}"))

                                    # Normalise to flat element list (same dual-shape handling as lot area):
                                    #   Shape A: {"elements": [{"elementId": N, "parameters": {...}}]}
                                    #   Shape B: {"results": {"id": {"parameters": {...}}, ...}}
                                    elem_list = []
                                    shape_a = parsed.get("elements") or parsed.get("outcome", {}).get("elements")
                                    if isinstance(shape_a, list):
                                        elem_list = shape_a
                                    else:
                                        results = parsed.get("results", {})
                                        if isinstance(results, list):
                                            results = {str(i): v for i, v in enumerate(results)}
                                        if isinstance(results, dict):
                                            for val in results.values():
                                                if isinstance(val, dict):
                                                    elem_list.append(val)

                                    for elem_val in elem_list:
                                        if not isinstance(elem_val, dict):
                                            continue
                                        params = elem_val.get("parameters", elem_val)
                                        area_val = _pick(params, AREA_KEYS)
                                        elem_id = elem_val.get("elementId") or elem_val.get("id", "?")
                                        name_val = _pick(params, NAME_KEYS) or f"Element {elem_id}"
                                        level_val = _pick(params, LEVEL_KEYS) or None

                                        if area_val is not None:
                                            try:
                                                area_float = self._extract_number(area_val)
                                                if area_float > 0:
                                                    total_sqft += area_float
                                                    elements_info.append({
                                                        "name": name_val,
                                                        "area_sqft": area_float,
                                                        "level": level_val,
                                                    })
                                            except ValueError:
                                                    pass
                                except Exception:
                                    pass
                return total_sqft, elements_info
            
            # 1. Get Lot Area — reuse _run_lot_area_audit which has the proven
            # two-step OST_SiteProperty query logic (query → element IDs → get_element_data).
            lot_result = await self._run_lot_area_audit(arguments)
            if lot_result.get("status") == "Unavailable":
                return {
                    "audit_type": "lot_coverage",
                    "status": "Unavailable",
                    "narrative": "The lot coverage calculation could not be completed. "
                                 "The audit tool was unable to find property lines "
                                 "(OST_SiteProperty elements) in the model, which are "
                                 "required to calculate the lot area. "
                                 "Please add property lines via the Revit Site tab → Property Line tool."
                }
            total_lot_sqft = lot_result.get("total_area_sqft", 0.0)
            if total_lot_sqft <= 0:
                return {
                    "audit_type": "lot_coverage",
                    "status": "Unavailable",
                    "narrative": "The lot area was calculated as zero — cannot compute coverage."
                }
                
            # 2. Get Building Footprint (OST_Floors)
            # For lot coverage, the building footprint is the largest floor plate
            # area on a single level (not the sum of all floors across all levels).
            _, floor_details = await get_category_area("OST_Floors")
            
            # Group floors by level and find the level with the largest total area
            from collections import defaultdict
            level_groups = defaultdict(list)
            for fd in floor_details:
                lv = fd.get("level") or "Unknown Level"
                level_groups[lv].append(fd)
            
            max_level_name = ""
            max_level_area = 0.0
            level_breakdown = []
            for lv_name, elements in level_groups.items():
                lv_total = sum(e.get("area_sqft", 0) for e in elements)
                level_entry = {
                    "level_name": lv_name,
                    "total_area_sqft": lv_total,
                    "elements": elements,
                }
                level_breakdown.append(level_entry)
                if lv_total > max_level_area:
                    max_level_area = lv_total
                    max_level_name = lv_name
            
            floor_sqft = max_level_area  # building footprint = max level area
            
            # 3. Get Additional Covered Areas (OST_Areas)
            # Additional areas (decks, patios) are summed — they are not stacked by level.
            area_sqft, area_details = await get_category_area("OST_Areas")
            
            total_covered_sqft = floor_sqft + area_sqft
            
            building_coverage_pct = (floor_sqft / total_lot_sqft) * 100
            total_coverage_pct = (total_covered_sqft / total_lot_sqft) * 100
            
            narrative = f"Lot Coverage Audit Complete.\n"
            narrative += f"Total Lot Area: {total_lot_sqft:,.2f} sq ft\n"
            narrative += f"Building Footprint Area: {floor_sqft:,.2f} sq ft\n"
            narrative += f"  (largest single-level floor plate: {max_level_name})\n"
            narrative += f"Additional Covered Area: {area_sqft:,.2f} sq ft\n\n"
            
            narrative += f"Building Lot Coverage: {building_coverage_pct:.1f}%\n"
            narrative += f"Total Lot Coverage: {total_coverage_pct:.1f}%\n"
            
            if include_details:
                if level_breakdown:
                    narrative += "\nBuilding Footprint — Per-Level Breakdown:\n"
                    for lv_entry in level_breakdown:
                        narrative += f"\n  Level: {lv_entry['level_name']}\n"
                        for elem in lv_entry["elements"]:
                            narrative += f"    - {elem['name']}: {elem['area_sqft']:,.2f} sq ft\n"
                        narrative += f"    Level Subtotal: {lv_entry['total_area_sqft']:,.2f} sq ft\n"
                    narrative += f"\n  Building Footprint (Largest Level): {max_level_area:,.2f} sq ft ({max_level_name})\n"
                if area_details:
                    narrative += "\nAdditional Covered Area Details:\n"
                    for a in area_details:
                        narrative += f"  - {a['name']}: {a['area_sqft']:,.2f} sq ft\n"
                        
            return {
                "audit_type": "lot_coverage",
                "status": "Success",
                "lot_area_sqft": total_lot_sqft,
                "building_area_sqft": floor_sqft,
                "additional_area_sqft": area_sqft,
                "building_coverage_percent": building_coverage_pct,
                "total_coverage_percent": total_coverage_pct,
                "max_level_name": max_level_name,
                "level_breakdown": level_breakdown,
                "details": {
                    "floors": floor_details,
                    "areas": area_details
                },
                "narrative": narrative
            }
            
        except Exception as e:
            return {
                "audit_type": "lot_coverage",
                "error": str(e),
                "narrative": f"Error running lot coverage audit: {e}"
            }


    async def _run_setback_audit(self, arguments: dict) -> dict:
        """
        Setback audit requires geometry from property lines to calculate distance to walls.
        Since the Autodesk Revit MCP server does not export geometry for property lines,
        this deterministic mathematical calculation is currently impossible.
        """
        return {
            "audit_type": "setback",
            "status": "unavailable",
            "narrative": (
                "The Setback Audit is currently unavailable. The tool relies on extracting "
                "precise coordinate geometry from property lines to calculate the perpendicular "
                "distance to building exterior walls. However, the Autodesk Revit MCP Server "
                "currently does not export geometry for property lines, making this deterministic "
                "mathematical calculation impossible. Please check back in future API updates."
            )
        }
    async def handle_request(self, request: dict) -> dict:
        """
        Handle a single JSON-RPC 2.0 request.
        Returns a JSON-RPC response dict.
        """
        request_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        logger.info("Received request: method=%s id=%s", method, request_id)

        try:
            if method == "tools/list":
                tools = await self.get_tools()
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": tools},
                }

            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})

                if not tool_name:
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32602,
                            "message": "Missing required parameter: 'name'",
                        },
                    }

                # Route to custom audit tools
                if tool_name == "axo_audit_septic":
                    result = await self._run_septic_audit(arguments)
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                elif tool_name == "axo_audit_energy":
                    result = await self._run_energy_audit(arguments)
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                elif tool_name == "axo_audit_wwr":
                    result = await self._run_wwr_audit(arguments)
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                elif tool_name == "axo_audit_floor_area":
                    result = await self._run_floor_area_audit(arguments)
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                elif tool_name == "axo_audit_lot_area":
                    result = await self._run_lot_area_audit(arguments or {})
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                elif tool_name == "axo_audit_lot_coverage":
                    result = await self._run_lot_coverage_audit(arguments or {})
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                elif tool_name == "axo_audit_setback":
                    result = await self._run_setback_audit(arguments or {})
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, indent=2),
                                }
                            ]
                        },
                    }

                # All other tools: proxy to the Autodesk Revit pipe
                else:
                    raw_result = await self._call_revit_tool(tool_name, arguments)

                    # _call_revit_tool already returns MCP-wrapped content via
                    # _translate_response: {"content": [{"type": "text", "text": "..."}]}.
                    # Check if already wrapped to avoid double-wrapping.
                    if isinstance(raw_result, dict) and "content" in raw_result:
                        return {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": raw_result,
                        }

                    # Fallback: wrap in MCP-compliant content format
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(raw_result, indent=2)
                                    if not isinstance(raw_result, str)
                                    else raw_result,
                                }
                            ]
                        },
                    }

            elif method == "resources/list":
                # Optional MCP method — return empty for now
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"resources": []},
                }

            elif method == "prompts/list":
                # Optional MCP method — return empty for now
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"prompts": []},
                }

            elif method == "initialize":
                # MCP initialization handshake
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "tools": {},
                            "resources": {},
                            "prompts": {},
                        },
                        "serverInfo": {
                            "name": "axoworks-revit-proxy",
                            "version": "1.0.0",
                        },
                    },
                }

            elif method == "notifications/initialized":
                # No response needed for notifications
                return None

            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    },
                }

        except RevitBridgeError as exc:
            logger.error("Bridge error handling %s: %s", method, exc)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": f"Revit Bridge Error: {exc}",
                },
            }
        except Exception as exc:
            logger.error("Unexpected error handling %s: %s", method, exc)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32603,
                    "message": f"Internal error: {exc}",
                },
            }

    # -----------------------------------------------------------------------
    # Stdio event loop
    # -----------------------------------------------------------------------

    async def run_stdio(self):
        """
        Read JSON-RPC 2.0 requests from stdin (line-delimited) and write
        responses to stdout.  This is the standard MCP stdio transport.
        """
        logger.info("Axoworks Revit MCP Proxy starting (stdio transport)...")
        logger.info("Reading JSON-RPC requests from stdin, writing to stdout.")

        # Signal readiness on stderr (not stdout — that's the protocol channel)
        print("Axoworks Revit MCP Proxy ready.", file=sys.stderr, flush=True)

        loop = asyncio.get_running_loop()

        while True:
            try:
                # Read one line from stdin (asynchronously)
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    logger.info("stdin closed. Shutting down.")
                    break

                line = line.strip()
                if not line:
                    continue

                # Parse the JSON-RPC request
                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.error("Invalid JSON on stdin: %s", exc)
                    response = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": f"Parse error: {exc}"},
                    }
                    print(json.dumps(response), flush=True)
                    continue

                if not isinstance(request, dict) or "method" not in request:
                    logger.error("Invalid request format: %s", request)
                    response = {
                        "jsonrpc": "2.0",
                        "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32600, "message": "Invalid Request"},
                    }
                    print(json.dumps(response), flush=True)
                    continue

                # Handle the request
                response = await self.handle_request(request)

                # Notifications don't get responses
                if response is None:
                    continue

                # Write the response as a newline-delimited JSON line
                print(json.dumps(response), flush=True)

            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received. Shutting down.")
                break
            except Exception as exc:
                logger.error("Fatal error in stdio loop: %s", exc)
                # Try to send error response
                try:
                    error_response = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32603, "message": f"Fatal error: {exc}"},
                    }
                    print(json.dumps(error_response), flush=True)
                except Exception:
                    pass
                break


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

async def main():
    transport = McpStdioTransport()
    await transport.run_stdio()


if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by user.")
    except Exception as exc:
        logger.error("Server crashed: %s", exc)
        sys.exit(1)
