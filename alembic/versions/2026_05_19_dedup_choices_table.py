"""dedup choices table

Revision ID: a1f2c3d4e5b6
Revises: c3dd24e4564d
Create Date: 2026-05-19 00:00:00.000000

"""

import json
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a1f2c3d4e5b6"
down_revision: Union[str, Sequence[str], None] = "c3dd24e4564d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _import_legacy_duplicates_json() -> None:
    """One-time import of the legacy data/duplicates.json into the new tables.

    No-ops when the file is absent (fresh machines; data/ is gitignored
    runtime-only).
    """
    from airdrome.conf import settings

    path = settings.duplicates_filepath
    if not path.exists():
        return

    data = json.loads(path.read_text(encoding="utf-8"))
    bind = op.get_bind()
    grp = sa.table("dedupgroup", sa.column("id", sa.Integer), sa.column("label", sa.String))
    mem = sa.table(
        "dedupgroupmember",
        sa.column("group_id", sa.Integer),
        sa.column("member_hash", sa.String),
        sa.column("canon_hash", sa.String),
    )

    n_groups = 0
    for key, saved in data.items():
        members = saved.get("members", [])
        canon_hashes = saved.get("canon_hashes", [])
        if len(members) != len(canon_hashes):
            continue
        gid = bind.execute(grp.insert().values(label=key).returning(grp.c.id)).scalar_one()
        bind.execute(
            mem.insert(),
            [{"group_id": gid, "member_hash": m, "canon_hash": c} for m, c in zip(members, canon_hashes)],
        )
        n_groups += 1
    print(f"imported {n_groups} dedup group(s) from {path}")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "dedupgroup",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "dedupgroupmember",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("member_hash", sa.String(), nullable=False),
        sa.Column("canon_hash", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["dedupgroup.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_dedupgroupmember_group_id"), "dedupgroupmember", ["group_id"], unique=False)
    op.create_index(
        op.f("ix_dedupgroupmember_member_hash"), "dedupgroupmember", ["member_hash"], unique=False
    )

    _import_legacy_duplicates_json()


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_dedupgroupmember_member_hash"), table_name="dedupgroupmember")
    op.drop_index(op.f("ix_dedupgroupmember_group_id"), table_name="dedupgroupmember")
    op.drop_table("dedupgroupmember")
    op.drop_table("dedupgroup")
