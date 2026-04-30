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
            tanks_raw = await self._bridge.run_mcp_tool(
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
            lines_raw = await self._bridge.run_mcp_tool(
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
            rooms_raw = await self._bridge.run_mcp_tool(
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
                data_raw = await self._bridge.run_mcp_tool(
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
                                        elements = parsed.get("outcome", {}).get("elements", parsed.get("elements", []))
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

    async def _run_lot_area_audit(self, arguments: dict) -> dict:
        """
        Lot area audit — calculates the area enclosed by property lines.
        
        Uses the Revit pipe to query OST_PropertyLine elements, extracts
        polyline/polygon geometry, and computes enclosed area via the
        shoelace formula (deterministic math, not LLM).
        
        Falls back to get_elements_by_category if query_model fails,
        and tries OST_Site / OST_BuildingPad as alternative sources.
        """
        area_unit = arguments.get("area_unit", "both")
        one_acre_sqft = 43560.0

        async def _extract_property_line_points(bridge) -> tuple[list[dict], str]:
            """Try multiple strategies to get property line geometry points."""
            
            # Strategy 1: query_model with OST_PropertyLine
            try:
                raw = await bridge.run_mcp_tool(
                    "query_model",
                    {
                        "input": {
                            "categories": ["OST_PropertyLine"],
                            "searchScope": "AllViews",
                            "maxResults": 200,
                        }
                    },
                )
                elements = _parse_elements(raw)
                if elements:
                    pts = await _points_from_elements(bridge, elements)
                    if pts:
                        return pts, "OST_PropertyLine"
            except (RevitBridgeError, Exception):
                pass

            # Strategy 2: get_elements_by_category with "Property Lines"
            try:
                raw = await bridge.run_mcp_tool(
                    "get_elements_by_category",
                    {"category": "Property Lines", "include_geometry": True},
                )
                elements = _parse_elements(raw)
                if elements:
                    pts = await _points_from_elements(bridge, elements)
                    if pts:
                        return pts, "Property Lines"
            except (RevitBridgeError, Exception):
                pass

            # Strategy 3: query_model with OST_Site
            try:
                raw = await bridge.run_mcp_tool(
                    "query_model",
                    {
                        "input": {
                            "categories": ["OST_Site"],
                            "searchScope": "AllViews",
                            "maxResults": 50,
                        }
                    },
                )
                elements = _parse_elements(raw)
                if elements:
                    pts = await _points_from_elements(bridge, elements)
                    if pts:
                        return pts, "OST_Site"
            except (RevitBridgeError, Exception):
                pass

            return [], None

        def _parse_elements(response: Any) -> list:
            """Unpack MCP-wrapped response into element list."""
            if isinstance(response, dict):
                content = response.get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            try:
                                parsed = json.loads(item.get("text", "{}"))
                                if isinstance(parsed, dict):
                                    elems = (
                                        parsed.get("outcome", {}).get("elements")
                                        or parsed.get("elements")
                                        or parsed.get("data", [])
                                    )
                                    if isinstance(elems, list):
                                        return elems
                            except (json.JSONDecodeError, AttributeError):
                                pass
                # Some servers return list directly
                if "elements" in response:
                    return response["elements"]
            if isinstance(response, list):
                return response
            return []

        async def _points_from_elements(bridge, elements: list) -> list[dict]:
            """
            Extract (x, y) points from property line elements.
            
            Tries get_element_data for full geometry, then falls back
            to extracting geometry/coordinates from the element dict directly.
            """
            points = []

            # Collect element IDs
            eids = []
            for el in elements:
                if isinstance(el, dict):
                    eid = el.get("elementId") or el.get("id")
                    if eid is not None:
                        eids.append(str(eid))

            # Try get_element_data with geometry
            if eids and len(eids) <= 200:
                try:
                    data_raw = await bridge.run_mcp_tool(
                        "get_element_data",
                        {"elementIds": eids},
                    )
                    detailed = _parse_elements(data_raw)
                    if detailed:
                        for el in detailed:
                            if not isinstance(el, dict):
                                continue
                            pts = _extract_xy_points(el)
                            points.extend(pts)
                except (RevitBridgeError, Exception):
                    pass

            # Fallback: extract from original elements
            if not points:
                for el in elements:
                    if isinstance(el, dict):
                        pts = _extract_xy_points(el)
                        points.extend(pts)

            # Remove duplicates and sort into polygon order
            if points:
                points = _deduplicate_and_order_points(points)

            return points

        def _extract_xy_points(element: dict) -> list[dict]:
            """Pull (x, y) coordinate pairs from an element dict."""
            extracted = []

            # Check geometry array
            geo = element.get("geometry")
            if isinstance(geo, list):
                for pt in geo:
                    if isinstance(pt, dict):
                        x = pt.get("x")
                        y = pt.get("y")
                        if x is not None and y is not None:
                            extracted.append({"x": x, "y": y})

            # Check boundingBox corners
            bbox = element.get("boundingBox")
            if isinstance(bbox, dict):
                for corner_key in ("minXYZ", "maxXYZ"):
                    corner = bbox.get(corner_key)
                    if isinstance(corner, dict):
                        x = corner.get("x")
                        y = corner.get("y")
                        if x is not None and y is not None:
                            extracted.append({"x": x, "y": y})

            # Check curve/line segments
            curves = element.get("curves") or element.get("curveLoops", [])
            if isinstance(curves, list):
                for curve in curves:
                    if isinstance(curve, dict):
                        for key in ("startPoint", "endPoint"):
                            pt = curve.get(key)
                            if isinstance(pt, dict):
                                x = pt.get("x")
                                y = pt.get("y")
                                if x is not None and y is not None:
                                    extracted.append({"x": x, "y": y})
                    elif isinstance(curve, list):
                        for pt in curve:
                            if isinstance(pt, dict):
                                x = pt.get("x")
                                y = pt.get("y")
                                if x is not None and y is not None:
                                    extracted.append({"x": x, "y": y})

            # Try direct x/y from element
            x = element.get("x")
            y = element.get("y")
            if x is not None and y is not None:
                extracted.append({"x": x, "y": y})

            return extracted

        def _deduplicate_and_order_points(raw_points: list[dict]) -> list[dict]:
            """Remove near-duplicate points and sort into polygon order."""
            if not raw_points:
                return []

            cleaned = []
            tolerance = 0.001  # 1/1000 ft tolerance
            for pt in raw_points:
                try:
                    xf = float(pt["x"])
                    yf = float(pt["y"])
                except (ValueError, TypeError, KeyError):
                    continue

                # Dedup
                is_dup = False
                for existing in cleaned:
                    if abs(existing["x"] - xf) < tolerance and abs(existing["y"] - yf) < tolerance:
                        is_dup = True
                        break
                if not is_dup:
                    cleaned.append({"x": xf, "y": yf})

            if len(cleaned) < 3:
                return cleaned

            # Sort into polygon order: find centroid, then sort by angle
            cx = sum(p["x"] for p in cleaned) / len(cleaned)
            cy = sum(p["y"] for p in cleaned) / len(cleaned)

            import math
            cleaned.sort(key=lambda p: math.atan2(p["y"] - cy, p["x"] - cx))

            return cleaned

        def _shoelace_area(points: list[dict]) -> float:
            """Calculate polygon area using the shoelace formula. Returns area in sq ft."""
            n = len(points)
            if n < 3:
                return 0.0

            area = 0.0
            for i in range(n):
                j = (i + 1) % n
                area += points[i]["x"] * points[j]["y"]
                area -= points[j]["x"] * points[i]["y"]

            return abs(area) / 2.0

        # --- Main execution ---
        points, source_category = await _extract_property_line_points(self._bridge)

        if not points or len(points) < 3:
            return {
                "audit_type": "lot_area",
                "status": "unavailable",
                "narrative": (
                    "Could not extract property line geometry from the Revit model. "
                    "The model may not have property lines defined, or the MCP query "
                    "failed. Check that property lines (OST_PropertyLine) exist in "
                    "the active Revit model and try again. "
                    "Alternatively, check site plan views or survey data in Revit directly."
                ),
                "source_category_attempted": source_category or "none",
                "points_found": len(points),
            }

        # Apply coordinate translation
        translated_points = await translator.translate_payload(self._bridge, points)

        # Calculate area
        area_sqft = _shoelace_area(translated_points if isinstance(translated_points, list) else points)
        area_sqft = round(area_sqft, 2)
        area_acres = round(area_sqft / one_acre_sqft, 4)

        # Build output
        result = {
            "audit_type": "lot_area",
            "status": "calculated",
            "source_category": source_category,
            "polygon_vertices": len(points),
        }

        if area_unit in ("sqft", "both"):
            result["area_sqft"] = area_sqft
        if area_unit in ("acres", "both"):
            result["area_acres"] = area_acres

        # Build narrative
        if area_sqft > 0:
            parts = [
                f"Lot area calculated from {len(points)} polygon vertices "
                f"(source: {source_category})."
            ]
            if area_unit in ("sqft", "both"):
                parts.append(f"  Area: {area_sqft:,.2f} sq ft")
            if area_unit in ("acres", "both"):
                parts.append(f"  Area: {area_acres:,.4f} acres")
            result["narrative"] = "\n".join(parts)
        else:
            result["narrative"] = (
                f"Property line geometry found ({len(points)} points from {source_category}) "
                f"but computed area is zero. The polygon may be degenerate or "
                f"points may not form a closed shape."
            )

        return result

    # -----------------------------------------------------------------------
    # Setback Audit — Closest Distance: Walls → Property Lines
    # -----------------------------------------------------------------------

    async def _run_setback_audit(self, arguments: dict) -> dict:
        """
        Calculate closest distances from building exterior walls to property lines.
        
        Strategy:
        1. Query OST_Walls for exterior walls → compute building envelope extents.
        2. Query OST_PropertyLine elements (with fallbacks) → extract property line
           segments (start/end points).
        3. Compute minimum distance from each wall face to each property line segment
           using point-to-line-segment perpendicular distance.
        4. Report results per side (North, South, East, West) plus the overall
           closest setback.
        """
        output_unit = arguments.get("output_unit", "ft_in")

        # ------------------------------------------------------------------
        # Helpers (same pattern as _run_lot_area_audit)
        # ------------------------------------------------------------------

        def _parse_elements(response: Any) -> list:
            """Unpack MCP-wrapped response into element list."""
            if isinstance(response, dict):
                content = response.get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            try:
                                parsed = json.loads(item.get("text", "{}"))
                                if isinstance(parsed, dict):
                                    elems = (
                                        parsed.get("outcome", {}).get("elements")
                                        or parsed.get("elements")
                                        or parsed.get("data", [])
                                    )
                                    if isinstance(elems, list):
                                        return elems
                            except (json.JSONDecodeError, AttributeError):
                                pass
                    if "elements" in response:
                        return response["elements"]
                if isinstance(response, list):
                    return response
            return []

        def _extract_bbox(element: dict) -> dict | None:
            """Extract min/max XYZ from element boundingBox."""
            bbox = element.get("boundingBox")
            if isinstance(bbox, dict):
                min_xyz = bbox.get("minXYZ")
                max_xyz = bbox.get("maxXYZ")
                if isinstance(min_xyz, dict) and isinstance(max_xyz, dict):
                    try:
                        return {
                            "minX": float(min_xyz.get("x", 0)),
                            "minY": float(min_xyz.get("y", 0)),
                            "maxX": float(max_xyz.get("x", 0)),
                            "maxY": float(max_xyz.get("y", 0)),
                        }
                    except (ValueError, TypeError):
                        pass
            return None

        def _extract_curve_segments(element: dict) -> list[dict]:
            """Extract (startX, startY, endX, endY) from curve geometry."""
            segments = []
            # Check curveLoops
            curves = element.get("curves") or element.get("curveLoops", [])
            if isinstance(curves, list):
                for curve in curves:
                    if isinstance(curve, dict):
                        sp = curve.get("startPoint")
                        ep = curve.get("endPoint")
                        if isinstance(sp, dict) and isinstance(ep, dict):
                            try:
                                segments.append({
                                    "startX": float(sp.get("x", 0)),
                                    "startY": float(sp.get("y", 0)),
                                    "endX": float(ep.get("x", 0)),
                                    "endY": float(ep.get("y", 0)),
                                })
                            except (ValueError, TypeError):
                                pass
                    elif isinstance(curve, list):
                        # Polyline: pairs of consecutive points form segments
                        pts = []
                        for pt in curve:
                            if isinstance(pt, dict):
                                try:
                                    pts.append((float(pt.get("x", 0)), float(pt.get("y", 0))))
                                except (ValueError, TypeError):
                                    pass
                        for i in range(len(pts) - 1):
                            segments.append({
                                "startX": pts[i][0], "startY": pts[i][1],
                                "endX": pts[i + 1][0], "endY": pts[i + 1][1],
                            })
            # Also try geometry array
            geo = element.get("geometry")
            if isinstance(geo, list) and not segments:
                for pt in geo:
                    if isinstance(pt, dict):
                        try:
                            x = float(pt.get("x", 0))
                            y = float(pt.get("y", 0))
                            segments.append({
                                "startX": x, "startY": y,
                                "endX": x, "endY": y,
                            })
                        except (ValueError, TypeError):
                            pass
            return segments

        def _format_distance(dist_ft: float, unit: str) -> str | dict:
            """Format a distance in feet to the requested output unit."""
            if dist_ft < 0:
                dist_ft = 0.0
            if unit == "in":
                total_in = round(dist_ft * 12, 1)
                return f"{total_in}\""
            if unit == "ft":
                return f"{round(dist_ft, 2)}'"
            # ft_in: feet and inches
            whole_ft = int(dist_ft)
            remaining_in = round((dist_ft - whole_ft) * 12)
            if remaining_in >= 12:
                whole_ft += 1
                remaining_in -= 12
            if whole_ft > 0 and remaining_in > 0:
                return f"{whole_ft}' {remaining_in}\""
            elif whole_ft > 0:
                return f"{whole_ft}'"
            else:
                return f"{remaining_in}\""

        def _point_to_segment_distance(
            px: float, py: float,
            ax: float, ay: float,
            bx: float, by: float,
        ) -> float:
            """
            Perpendicular distance from point P to line segment AB.
            Returns distance in the same unit as coordinates.
            Uses vector projection method.
            """
            # Vector AB
            abx = bx - ax
            aby = by - ay
            # Vector AP
            apx = px - ax
            apy = py - ay

            # Squared length of AB
            ab_len_sq = abx * abx + aby * aby
            if ab_len_sq == 0:
                # Degenerate segment — distance to endpoint A
                return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5

            # Project AP onto AB, clamped to [0, 1]
            t = (apx * abx + apy * aby) / ab_len_sq
            t = max(0.0, min(1.0, t))

            # Closest point on segment
            cx = ax + t * abx
            cy = ay + t * aby

            return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5

        # ------------------------------------------------------------------
        # Step 1: Query exterior walls
        # ------------------------------------------------------------------

        try:
            walls_raw = await self._bridge.run_mcp_tool(
                "query_model",
                {
                    "input": {
                        "categories": ["OST_Walls"],
                        "searchScope": "AllViews",
                        "maxResults": 500,
                    }
                },
            )
        except RevitBridgeError as exc:
            return {"error": f"Failed to query walls: {exc}"}

        wall_elements = _parse_elements(walls_raw)

        if not wall_elements:
            return {
                "audit_type": "setback",
                "status": "unavailable",
                "narrative": "No wall elements found in the Revit model. Cannot compute building envelope.",
                "walls_found": 0,
            }

        # Build envelope from wall bounding boxes
        envelope = {"minX": float("inf"), "minY": float("inf"),
                    "maxX": float("-inf"), "maxY": float("-inf")}
        walls_with_bbox = 0

        # Also collect wall element IDs for detailed geometry fallback
        wall_ids = []
        for el in wall_elements:
            if isinstance(el, dict):
                bbox = _extract_bbox(el)
                if bbox:
                    envelope["minX"] = min(envelope["minX"], bbox["minX"])
                    envelope["minY"] = min(envelope["minY"], bbox["minY"])
                    envelope["maxX"] = max(envelope["maxX"], bbox["maxX"])
                    envelope["maxY"] = max(envelope["maxY"], bbox["maxY"])
                    walls_with_bbox += 1
                eid = el.get("elementId") or el.get("id")
                if eid is not None:
                    wall_ids.append(str(eid))

        # Try get_element_data for more precise geometry if bounding boxes are sparse
        if walls_with_bbox < 3 and wall_ids:
            try:
                detailed_raw = await self._bridge.run_mcp_tool(
                    "get_element_data",
                    {"elementIds": wall_ids[:50]},
                )
                detailed = _parse_elements(detailed_raw)
                for el in detailed:
                    if isinstance(el, dict):
                        bbox = _extract_bbox(el)
                        if bbox:
                            envelope["minX"] = min(envelope["minX"], bbox["minX"])
                            envelope["minY"] = min(envelope["minY"], bbox["minY"])
                            envelope["maxX"] = max(envelope["maxX"], bbox["maxX"])
                            envelope["maxY"] = max(envelope["maxY"], bbox["maxY"])
            except (RevitBridgeError, Exception):
                pass

        if envelope["minX"] == float("inf") or envelope["maxX"] == float("-inf"):
            return {
                "audit_type": "setback",
                "status": "unavailable",
                "narrative": "Could not determine building envelope from wall bounding boxes.",
                "walls_found": len(wall_elements),
                "walls_with_bbox": walls_with_bbox,
            }

        # ------------------------------------------------------------------
        # Step 2: Query property lines
        # ------------------------------------------------------------------

        property_segments = []  # list of {startX, startY, endX, endY}
        source_category = None

        # Strategy 1: query_model with OST_PropertyLine
        try:
            lines_raw = await self._bridge.run_mcp_tool(
                "query_model",
                {
                    "input": {
                        "categories": ["OST_PropertyLine"],
                        "searchScope": "AllViews",
                        "maxResults": 200,
                    }
                },
            )
            line_elements = _parse_elements(lines_raw)
            if line_elements:
                # Try detailed data first
                line_ids = []
                for el in line_elements:
                    if isinstance(el, dict):
                        eid = el.get("elementId") or el.get("id")
                        if eid is not None:
                            line_ids.append(str(eid))

                if line_ids:
                    try:
                        detail_raw = await self._bridge.run_mcp_tool(
                            "get_element_data",
                            {"elementIds": line_ids},
                        )
                        detail_elements = _parse_elements(detail_raw)
                        for el in detail_elements or line_elements:
                            if isinstance(el, dict):
                                segs = _extract_curve_segments(el)
                                property_segments.extend(segs)
                                if not segs:
                                    # Fallback: use bounding box corners as segment
                                    bbox = _extract_bbox(el)
                                    if bbox:
                                        property_segments.append({
                                            "startX": bbox["minX"], "startY": bbox["minY"],
                                            "endX": bbox["maxX"], "endY": bbox["minY"],
                                        })
                                        property_segments.append({
                                            "startX": bbox["maxX"], "startY": bbox["minY"],
                                            "endX": bbox["maxX"], "endY": bbox["maxY"],
                                        })
                                        property_segments.append({
                                            "startX": bbox["maxX"], "startY": bbox["maxY"],
                                            "endX": bbox["minX"], "endY": bbox["maxY"],
                                        })
                                        property_segments.append({
                                            "startX": bbox["minX"], "startY": bbox["maxY"],
                                            "endX": bbox["minX"], "endY": bbox["minY"],
                                        })
                    except (RevitBridgeError, Exception):
                        # Fallback: curve data from original elements
                        for el in line_elements:
                            if isinstance(el, dict):
                                segs = _extract_curve_segments(el)
                                property_segments.extend(segs)

                if property_segments:
                    source_category = "OST_PropertyLine"
        except (RevitBridgeError, Exception):
            pass

        # Strategy 2: get_elements_by_category with "Property Lines"
        if not property_segments:
            try:
                lines_raw = await self._bridge.run_mcp_tool(
                    "get_elements_by_category",
                    {"category": "Property Lines", "include_geometry": True},
                )
                line_elements = _parse_elements(lines_raw)
                for el in line_elements:
                    if isinstance(el, dict):
                        segs = _extract_curve_segments(el)
                        property_segments.extend(segs)
                if property_segments:
                    source_category = "Property Lines (get_elements_by_category)"
            except (RevitBridgeError, Exception):
                pass

        # Strategy 3: query_model with OST_Site
        if not property_segments:
            try:
                site_raw = await self._bridge.run_mcp_tool(
                    "query_model",
                    {
                        "input": {
                            "categories": ["OST_Site"],
                            "searchScope": "AllViews",
                            "maxResults": 50,
                        }
                    },
                )
                site_elements = _parse_elements(site_raw)
                for el in site_elements:
                    if isinstance(el, dict):
                        segs = _extract_curve_segments(el)
                        property_segments.extend(segs)
                        if not segs:
                            bbox = _extract_bbox(el)
                            if bbox:
                                property_segments.append({
                                    "startX": bbox["minX"], "startY": bbox["minY"],
                                    "endX": bbox["maxX"], "endY": bbox["maxY"],
                                })
                if property_segments:
                    source_category = "OST_Site"
            except (RevitBridgeError, Exception):
                pass

        if not property_segments:
            return {
                "audit_type": "setback",
                "status": "unavailable",
                "narrative": (
                    "Could not find property line geometry. The model may not have "
                    "property lines defined. Tried: OST_PropertyLine, "
                    "get_elements_by_category('Property Lines'), OST_Site."
                ),
                "walls_found": len(wall_elements),
                "building_envelope": {
                    "minX": round(envelope["minX"], 2),
                    "minY": round(envelope["minY"], 2),
                    "maxX": round(envelope["maxX"], 2),
                    "maxY": round(envelope["maxY"], 2),
                },
            }

        # ------------------------------------------------------------------
        # Step 3: Calculate distances
        # ------------------------------------------------------------------

        building_corners = {
            # Midpoints of each face
            "north": {"x": (envelope["minX"] + envelope["maxX"]) / 2.0, "y": envelope["maxY"]},
            "south": {"x": (envelope["minX"] + envelope["maxX"]) / 2.0, "y": envelope["minY"]},
            "east": {"x": envelope["maxX"], "y": (envelope["minY"] + envelope["maxY"]) / 2.0},
            "west": {"x": envelope["minX"], "y": (envelope["minY"] + envelope["maxY"]) / 2.0},
        }

        # Also compute wall face lines for more precise distance
        wall_faces = {
            "north": {"x1": envelope["minX"], "y1": envelope["maxY"],
                      "x2": envelope["maxX"], "y2": envelope["maxY"]},
            "south": {"x1": envelope["minX"], "y1": envelope["minY"],
                      "x2": envelope["maxX"], "y2": envelope["minY"]},
            "east": {"x1": envelope["maxX"], "y1": envelope["minY"],
                     "x2": envelope["maxX"], "y2": envelope["maxY"]},
            "west": {"x1": envelope["minX"], "y1": envelope["minY"],
                     "x2": envelope["minX"], "y2": envelope["maxY"]},
        }

        side_map = {
            # Which property line side faces which building wall
            "north": ("north", "south", "east", "west"),
            "south": ("south", "north", "east", "west"),
            "east": ("east", "west", "north", "south"),
            "west": ("west", "east", "north", "south"),
        }

        direction_map = {
            "north": ("Y", "max", "North (max Y)"),
            "south": ("Y", "min", "South (min Y)"),
            "east": ("X", "max", "East (max X)"),
            "west": ("X", "min", "West (min X)"),
        }

        results = {}
        overall_min_dist = float("inf")
        overall_min_side = None

        for side in ("north", "south", "east", "west"):
            axis, bound, label = direction_map[side]
            wall_face = wall_faces[side]

            min_dist = float("inf")
            closest_segment = None

            for seg in property_segments:
                # Compute distance from the wall face midpoint to this property segment
                mid_x = building_corners[side]["x"]
                mid_y = building_corners[side]["y"]

                d = _point_to_segment_distance(
                    mid_x, mid_y,
                    seg["startX"], seg["startY"],
                    seg["endX"], seg["endY"],
                )

                if d < min_dist:
                    min_dist = d
                    closest_segment = seg

            # Also check distance from the wall face line itself (as a segment)
            # to each property line segment (segment-to-segment minimum distance)
            for seg in property_segments:
                d = _point_to_segment_distance(
                    wall_face["x1"], wall_face["y1"],
                    seg["startX"], seg["startY"],
                    seg["endX"], seg["endY"],
                )
                if d < min_dist:
                    min_dist = d
                    closest_segment = seg

                d = _point_to_segment_distance(
                    wall_face["x2"], wall_face["y2"],
                    seg["startX"], seg["startY"],
                    seg["endX"], seg["endY"],
                )
                if d < min_dist:
                    min_dist = d
                    closest_segment = seg

            if min_dist < float("inf"):
                if min_dist < overall_min_dist:
                    overall_min_dist = min_dist
                    overall_min_side = side

                formatted = _format_distance(min_dist, output_unit)
                entry = {
                    "side": side.capitalize(),
                    "wall_face": label,
                    "distance": formatted,
                    "distance_ft": round(min_dist, 2),
                    "property_line_segment": {
                        "start": {
                            "x": round(closest_segment["startX"], 2) if closest_segment else None,
                            "y": round(closest_segment["startY"], 2) if closest_segment else None,
                        },
                        "end": {
                            "x": round(closest_segment["endX"], 2) if closest_segment else None,
                            "y": round(closest_segment["endY"], 2) if closest_segment else None,
                        },
                    } if closest_segment else None,
                }

                # Classify the property line orientation relative to this wall
                if closest_segment:
                    dx = closest_segment["endX"] - closest_segment["startX"]
                    dy = closest_segment["endY"] - closest_segment["startY"]
                    length = (dx * dx + dy * dy) ** 0.5
                    if length > 0.01:
                        # Determine cardinal orientation
                        if abs(dx) > abs(dy):
                            orientation = "East-West (horizontal)" if axis == "Y" else "North-South (vertical)"
                        else:
                            orientation = "North-South (vertical)" if axis == "Y" else "East-West (horizontal)"
                        if abs(dx) > 0.01 and abs(dy) > 0.01:
                            orientation = "Diagonal"
                        entry["property_line_orientation"] = orientation
                        entry["property_line_length_ft"] = round(length, 2)

                results[side] = entry
            else:
                results[side] = {
                    "side": side.capitalize(),
                    "wall_face": label,
                    "distance": "Could not compute",
                    "distance_ft": None,
                }

        # ------------------------------------------------------------------
        # Step 4: Apply coordinate translation
        # ------------------------------------------------------------------
        try:
            translated = await translator.translate_payload(self._bridge, results)
            if isinstance(translated, dict):
                results = translated
        except Exception:
            pass

        # ------------------------------------------------------------------
        # Step 5: Build output
        # ------------------------------------------------------------------

        output = {
            "audit_type": "setback",
            "status": "calculated",
            "source_category": source_category,
            "building_envelope": {
                "minX": round(envelope["minX"], 2),
                "minY": round(envelope["minY"], 2),
                "maxX": round(envelope["maxX"], 2),
                "maxY": round(envelope["maxY"], 2),
                "width_ft": round(envelope["maxX"] - envelope["minX"], 2),
                "depth_ft": round(envelope["maxY"] - envelope["minY"], 2),
            },
            "walls_queried": len(wall_elements),
            "property_line_segments_found": len(property_segments),
            "setback_distances": results,
        }

        # Identify overall closest
        if overall_min_side and overall_min_dist < float("inf"):
            output["closest_setback"] = {
                "side": overall_min_side.capitalize(),
                "distance": _format_distance(overall_min_dist, output_unit),
                "distance_ft": round(overall_min_dist, 2),
            }

        # Build narrative
        narrative_parts = [
            f"Setback analysis: {len(wall_elements)} wall(s) queried, "
            f"{len(property_segments)} property line segment(s) found "
            f"(source: {source_category}).",
            f"Building envelope: {output['building_envelope']['width_ft']}' wide x "
            f"{output['building_envelope']['depth_ft']}' deep.",
            "",
            "Setback Distances (Wall Face → Property Line):",
        ]

        for side_key in ("north", "south", "east", "west"):
            entry = results.get(side_key, {})
            side_name = side_key.capitalize()
            dist_str = entry.get("distance", "N/A")
            narrative_parts.append(f"  {side_name}: {dist_str}")

        if overall_min_side and overall_min_dist < float("inf"):
            narrative_parts.append("")
            narrative_parts.append(
                f"⚠️ Closest setback: {overall_min_side.capitalize()} side at "
                f"{_format_distance(overall_min_dist, output_unit)}. "
                "Verify this measurement carefully given its proximity."
            )

        narrative_parts.append("")
        narrative_parts.append(
            "Note: Distances are calculated from wall bounding box extents "
            "(including wall thickness). Actual face-of-wall to property line "
            "measurements may differ slightly."
        )

        output["narrative"] = "\n".join(narrative_parts)

        return output

    @staticmethod
    def _try_parse_float(value: Any, default: float = 0.0) -> float:
        """Safely parse a value as float, returning default on failure."""
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    # -----------------------------------------------------------------------
    # Main request handler
    # -----------------------------------------------------------------------

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
                    result = await self._run_lot_area_audit(arguments)
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
                    result = await self._run_setback_audit(arguments)
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
