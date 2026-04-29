from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator

class RevitPoint(BaseModel):
    x: float
    y: float
    z: Optional[float] = None

class RevitElement(BaseModel):
    element_id: int
    category: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)
    geometry: Optional[List[RevitPoint]] = None

class EnergyExtraction(BaseModel):
    wall_max_u: Optional[float] = Field(None, gt=0)
    roof_max_u: Optional[float] = Field(None, gt=0)
    window_max_u: Optional[float] = Field(None, gt=0)
    window_max_shgc: Optional[float] = Field(None, ge=0, le=1)
    still_missing: List[str] = Field(default_factory=list)

    @field_validator("still_missing", mode="before")
    @classmethod
    def ensure_list(cls, v):
        return v if isinstance(v, list) else []
