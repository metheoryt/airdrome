from .auto import (
    AutoDedupResult,
    DedupPlan,
    apply_plan,
    auto_deduplicate,
    compute_auto_dedup_groups,
    plan_dedup,
)
from .grouping import FIELDS, RECOMMENDED_SETS, CanonStrategy, canon_order, flag_set
from .mirror import install_mirror, restore_if_empty, write_mirror
from .persistence import flatten_canon_chains, recompute_main_files
from .recording import compute_recording_groups
from .store import ImportCounts, import_decisions, import_legacy


__all__ = [
    "FIELDS",
    "RECOMMENDED_SETS",
    "AutoDedupResult",
    "CanonStrategy",
    "DedupPlan",
    "ImportCounts",
    "apply_plan",
    "auto_deduplicate",
    "canon_order",
    "compute_auto_dedup_groups",
    "compute_recording_groups",
    "flag_set",
    "flatten_canon_chains",
    "import_decisions",
    "import_legacy",
    "install_mirror",
    "plan_dedup",
    "recompute_main_files",
    "restore_if_empty",
    "write_mirror",
]
