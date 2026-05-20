from .auto import AutoDedupResult, auto_deduplicate, compute_auto_dedup_groups
from .manual import Deduplicator
from .persistence import apply_manual_overrides, flatten_canon_chains
from .tui import DeduplicatorUI


__all__ = [
    "AutoDedupResult",
    "Deduplicator",
    "DeduplicatorUI",
    "apply_manual_overrides",
    "auto_deduplicate",
    "compute_auto_dedup_groups",
    "flatten_canon_chains",
]
