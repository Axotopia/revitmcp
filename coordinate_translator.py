"""
Coordinate Translation Layer for Revit MCP
Translates Global Z (Internal Origin) to Project Z (Project Base Point).
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger("revitmcp.translator")

class CoordinateTranslator:
    _instance = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._offset_z = None
            cls._instance._initialized = False
        return cls._instance

    async def get_project_z_offset(self, bridge: Any) -> float:
        async with self._lock:
            if self._offset_z is not None:
                return self._offset_z
            
            self._offset_z = 0.0  # Default fallback
            
            # Access the raw RevitBridge to avoid governor recursive translation deadlock
            raw_bridge = getattr(bridge, '_bridge', bridge)
            
            try:
                # Discover available tools
                res = await raw_bridge.list_mcp_tools()
                tools = [t.get("name") for t in res] if isinstance(res, list) else []
                
                base_point_element = None

                # Try query_model if available
                if "query_model" in tools:
                    try:
                        resp = await raw_bridge.run_mcp_tool(
                            "query_model", 
                            {"input": {"categoryNames": ["OST_ProjectBasePoint"]}}
                        )
                        if isinstance(resp, dict) and "elements" in resp:
                            elements = resp["elements"]
                            if elements:
                                base_point_element = elements[0]
                    except Exception as e:
                        logger.debug(f"query_model failed for BasePoint: {e}")

                # Try get_elements_by_category if query_model didn't work
                if base_point_element is None and "get_elements_by_category" in tools:
                    for cat in ["Project Base Point", "BasePoints", "OST_ProjectBasePoint"]:
                        try:
                            resp = await raw_bridge.run_mcp_tool(
                                "get_elements_by_category", 
                                {"category": cat, "include_geometry": False}
                            )
                            elements = None
                            if isinstance(resp, list):
                                elements = resp
                            elif isinstance(resp, dict) and "elements" in resp:
                                elements = resp["elements"]
                            
                            if elements and len(elements) > 0:
                                base_point_element = elements[0]
                                break
                        except Exception:
                            continue
                            
                if base_point_element:
                    # Extract Elevation parameter
                    params = base_point_element.get("parameters", {})
                    z_val = None
                    
                    # Search inside parameters
                    for key, val in params.items():
                        if key.lower() in ["elev", "elevation", "z", "z_offset"]:
                            try:
                                z_val = float(val)
                                break
                            except (ValueError, TypeError):
                                pass
                                
                    # Search root element if not found
                    if z_val is None:
                        for key, val in base_point_element.items():
                            if key.lower() in ["elev", "elevation", "z", "z_offset"]:
                                try:
                                    z_val = float(val)
                                    break
                                except (ValueError, TypeError):
                                    pass

                    if z_val is not None:
                        self._offset_z = z_val
                        logger.info(f"Initialized Coordinate Translator with Project Z Offset: {self._offset_z}")

            except Exception as e:
                logger.error(f"Failed to fetch Project Base Point: {e}")

            return self._offset_z

    def _translate_dict(self, data: dict, offset_z: float) -> dict:
        # Handle "geometry" arrays
        if "geometry" in data and isinstance(data["geometry"], list):
            for pt in data["geometry"]:
                if isinstance(pt, dict) and "z" in pt:
                    try:
                        pt["z"] = float(pt["z"]) - offset_z
                    except (ValueError, TypeError):
                        pass
                        
        # Handle "boundingBox"
        if "boundingBox" in data and isinstance(data["boundingBox"], dict):
            bbox = data["boundingBox"]
            if "minXYZ" in bbox and isinstance(bbox["minXYZ"], dict) and "z" in bbox["minXYZ"]:
                try:
                    bbox["minXYZ"]["z"] = float(bbox["minXYZ"]["z"]) - offset_z
                except (ValueError, TypeError):
                    pass
            if "maxXYZ" in bbox and isinstance(bbox["maxXYZ"], dict) and "z" in bbox["maxXYZ"]:
                try:
                    bbox["maxXYZ"]["z"] = float(bbox["maxXYZ"]["z"]) - offset_z
                except (ValueError, TypeError):
                    pass
                    
        # Handle pure XYZ point dicts directly if they stand alone
        if "x" in data and "y" in data and "z" in data:
            try:
                data["z"] = float(data["z"]) - offset_z
            except (ValueError, TypeError):
                pass
                
        # Recursively apply to all nested dictionaries and lists
        for key, value in data.items():
            if isinstance(value, dict):
                self._translate_dict(value, offset_z)
            elif isinstance(value, list):
                self._translate_list(value, offset_z)

        return data

    def _translate_list(self, data: list, offset_z: float) -> list:
        for item in data:
            if isinstance(item, dict):
                self._translate_dict(item, offset_z)
            elif isinstance(item, list):
                self._translate_list(item, offset_z)
        return data

    async def translate_payload(self, bridge: Any, payload: Any) -> Any:
        """
        Translates all Z coordinates in the payload from Global Z to Project Z.
        """
        if not payload:
            return payload

        offset_z = await self.get_project_z_offset(bridge)
        if offset_z == 0.0:
            return payload

        # Deep copy is not strictly necessary if we translate in-place on the final payload before returning
        if isinstance(payload, dict):
            self._translate_dict(payload, offset_z)
        elif isinstance(payload, list):
            self._translate_list(payload, offset_z)
            
        return payload

# Singleton instance for easy importing
translator = CoordinateTranslator()
