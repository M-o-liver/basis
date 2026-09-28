from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import json
import math

@dataclass(frozen=True)
class Config:
    catalog_seconds: int = 180
    options_seconds: int = 15
    yahoo_seconds: int = 60
    history_seconds: int = 300
    analysis_seconds: int = 60
    pm_max_age_ms: int = 90_000
    gamma_max_age_ms: int = 240_000
    options_max_age_ms: int = 45_000
    spot_max_age_ms: int = 30_000
    history_max_age_ms: int = 360_000
    max_expiry_offset_hours: int = 72
    open_gap_pp: float = 4.0
    close_gap_pp: float = 1.0
    stable_pp: float = 0.02
    fast_pp_per_second: float = 0.05
    event_jump_pp: float = 0.25
    spot_jump_return: float = 0.0025
    max_pm_spread: float = 0.10
    max_events: int = 60
    max_per_asset: int = 20
    analysis_window: int = 64
    analysis_grid_seconds: int = 15
    analysis_scales: tuple = (5, 15, 30, 60, 300, 1800)
    yahoo_enabled: bool = True
    min_free_mb: int = 256

    def __post_init__(self):
        for name, value in asdict(self).items():
            if isinstance(value, (int, float)) and not math.isfinite(value):
                raise ValueError(f'{name} must be finite')
        if not 0 <= self.close_gap_pp < self.open_gap_pp <= 100:
            raise ValueError('Require 0 <= close_gap_pp < open_gap_pp <= 100')
        for name in ('catalog_seconds', 'options_seconds', 'yahoo_seconds', 'history_seconds', 'analysis_seconds', 'analysis_grid_seconds', 'max_events', 'max_per_asset', 'analysis_window', 'min_free_mb', 'pm_max_age_ms', 'gamma_max_age_ms', 'options_max_age_ms', 'spot_max_age_ms', 'history_max_age_ms'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f'{name} must be a positive integer')
        if self.analysis_window < 8 or not self.analysis_scales or any(not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0 for x in self.analysis_scales):
            raise ValueError('Analysis needs window >= 8 and positive finite scales')
        for name in ('stable_pp', 'fast_pp_per_second', 'event_jump_pp', 'spot_jump_return'):
            if getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive')
        if self.max_expiry_offset_hours < 0:
            raise ValueError('max_expiry_offset_hours cannot be negative')
        if not 0 < self.max_pm_spread <= 1:
            raise ValueError('max_pm_spread must be in (0,1]')

    @property
    def hash(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]

    @classmethod
    def load(cls, path=None):
        data = json.loads(Path(path).read_text()) if path else {}
        if 'analysis_scales' in data:
            data['analysis_scales'] = tuple(data['analysis_scales'])
        return cls(**data)
