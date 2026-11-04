"""
NOVA Compute — Resource Policy Configuration
Defines configurable policy rules, thresholds, and execution modes.
Policies are fully configurable and never hardcoded in the collector logic.
"""

from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional
import json
import os
from .models import ExecutionMode

@dataclass
class ResourcePolicy:
    minimum_inference_ram_gib: float = 10.0
    preferred_inference_ram_gib: float = 12.0
    preferred_inference_ram_target_gib: float = 13.0
    workspace_reserve_gib: float = 4.0
    critical_ram_gib: float = 2.0
    minimum_vram_mib: float = 5500.0
    minimum_disk_free_gib: float = 25.0
    sampling_interval_ms: int = 500
    low_ram_abort_duration_seconds: float = 5.0
    critical_commit_pct: float = 90.0
    critical_vram_free_mib: float = 512.0
    child_termination_grace_period_seconds: float = 5.0
    execution_mode: ExecutionMode = ExecutionMode.WORKSPACE_SAFE
    # Adaptive Memory Reclaimer (AMR) configurations
    reclamation_cooldown_seconds: float = 30.0
    min_reclamation_delta_mb: float = 10.0
    auto_reclaim_enabled: bool = True

    @property
    def ram_pressure_optimization_gib(self) -> float:
        """Threshold below which proactive AMR optimization is triggered."""
        return self.preferred_inference_ram_gib

    @property
    def ram_pressure_constrained_gib(self) -> float:
        """Threshold below which system is memory-constrained (fails admission gate)."""
        return self.minimum_inference_ram_gib

    @property
    def ram_pressure_critical_gib(self) -> float:
        """Threshold below which system is in critical memory pressure."""
        return self.critical_ram_gib

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["execution_mode"] = self.execution_mode.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResourcePolicy":
        data_copy = dict(data)
        if "execution_mode" in data_copy and isinstance(data_copy["execution_mode"], str):
            data_copy["execution_mode"] = ExecutionMode(data_copy["execution_mode"])
        return cls(**data_copy)

    @classmethod
    def load_from_file(cls, path: str) -> "ResourcePolicy":
        if not os.path.exists(path):
            raise FileNotFoundError(f"Policy file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    def save_to_file(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

DEFAULT_POLICY = ResourcePolicy()
