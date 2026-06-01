from .auto import AutoDedupResult, auto_deduplicate, compute_auto_dedup_groups
from .grouping import CanonStrategy, canon_order
from .manual import Deduplicator
from .persistence import (
    apply_manual_overrides,
    export_dedup_groups,
    flatten_canon_chains,
    import_dedup_groups,
)
from .tui import DeduplicatorUI


__all__ = [
    "AutoDedupResult",
    "CanonStrategy",
    "Deduplicator",
    "DeduplicatorUI",
    "apply_manual_overrides",
    "auto_deduplicate",
    "canon_order",
    "compute_auto_dedup_groups",
    "export_dedup_groups",
    "flatten_canon_chains",
    "import_dedup_groups",
]
