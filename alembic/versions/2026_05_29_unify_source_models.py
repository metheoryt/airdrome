"""unify source models

Replaces the per-provider Apple track/playlist tables (appletrack, apple_ms_track,
appleplaylist, apple_ms_playlist + join tables) with provider-marked source_track /
source_playlist / source_playlist_track tables carrying a JSONB `extra` blob.

Data is not migrated — re-import from the original exports (imports are idempotent).

Revision ID: b2c3d4e5f6a7
Revises: a1f2c3d4e5b6
Create Date: 2026-05-29 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1f2c3d4e5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PROVIDER = sa.Enum("APPLE_XML", "APPLE_MS", name="provider", native_enum=False)


def upgrade() -> None:
    """Upgrade schema."""
    # Drop the old per-provider Apple tables (join tables first for FK order).
    op.drop_table("apple_ms_playlist_track")
    op.drop_table("appleplaylisttrack")
    op.drop_table("appletrack")
    op.drop_table("apple_ms_track")
    op.drop_table("appleplaylist")
    op.drop_table("apple_ms_playlist")
    # `appletrack.kind` was a native PG enum; the type is orphaned once the table is gone.
    sa.Enum(name="kind").drop(op.get_bind(), checkfirst=True)

    op.create_table(
        "source_track",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", _PROVIDER, nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("artist", sa.String(), nullable=True),
        sa.Column("album", sa.String(), nullable=True),
        sa.Column("album_artist", sa.String(), nullable=True),
        sa.Column("compilation", sa.Boolean(), nullable=True),
        sa.Column("track_number", sa.Integer(), nullable=True),
        sa.Column("disc_number", sa.Integer(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("date_added", sa.DateTime(timezone=True), nullable=True),
        sa.Column("date_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("loved", sa.Boolean(), nullable=False),
        sa.Column("album_loved", sa.Boolean(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("rating_computed", sa.Boolean(), nullable=False),
        sa.Column("album_rating", sa.Integer(), nullable=True),
        sa.Column("album_rating_computed", sa.Boolean(), nullable=False),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["track_id"], ["track.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "source_id"),
    )
    op.create_index(op.f("ix_source_track_track_id"), "source_track", ["track_id"], unique=False)

    op.create_table(
        "source_playlist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", _PROVIDER, nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("date_added", sa.DateTime(timezone=True), nullable=True),
        sa.Column("date_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("folder", sa.Boolean(), nullable=False),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "source_id"),
    )

    op.create_table(
        "source_playlist_track",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("playlist_id", sa.Integer(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["playlist_id"], ["source_playlist.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["track_id"], ["source_track.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_source_playlist_track_playlist_id"), "source_playlist_track", ["playlist_id"], unique=False
    )
    op.create_index(
        op.f("ix_source_playlist_track_position"), "source_playlist_track", ["position"], unique=False
    )
    op.create_index(
        op.f("ix_source_playlist_track_track_id"), "source_playlist_track", ["track_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema — recreate the original per-provider Apple tables (empty)."""
    op.drop_index(op.f("ix_source_playlist_track_track_id"), table_name="source_playlist_track")
    op.drop_index(op.f("ix_source_playlist_track_position"), table_name="source_playlist_track")
    op.drop_index(op.f("ix_source_playlist_track_playlist_id"), table_name="source_playlist_track")
    op.drop_table("source_playlist_track")
    op.drop_table("source_playlist")
    op.drop_index(op.f("ix_source_track_track_id"), table_name="source_track")
    op.drop_table("source_track")

    op.create_table(
        "apple_ms_playlist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("container_identifier", sa.BIGINT(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("container_type", sa.String(), nullable=False),
        sa.Column("parent_folder_identifier", sa.BIGINT(), nullable=True),
        sa.Column("date_added", sa.DateTime(timezone=True), nullable=True),
        sa.Column("items_modified_date", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("container_identifier"),
    )
    op.create_table(
        "appleplaylist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("playlist_id", sa.Integer(), nullable=False),
        sa.Column("persistent_id", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("all_items", sa.Boolean(), nullable=False),
        sa.Column("parent_persistent_id", sa.String(), nullable=True),
        sa.Column("master", sa.Boolean(), nullable=False),
        sa.Column("visible", sa.Boolean(), nullable=False),
        sa.Column("music", sa.Boolean(), nullable=False),
        sa.Column("folder", sa.Boolean(), nullable=False),
        sa.Column("distinguished_kind", sa.Integer(), nullable=True),
        sa.Column("favorited", sa.Boolean(), nullable=False),
        sa.Column("loved", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("persistent_id"),
        sa.UniqueConstraint("playlist_id"),
    )
    op.create_table(
        "apple_ms_track",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=True),
        sa.Column("track_identifier", sa.BIGINT(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("artist", sa.String(), nullable=True),
        sa.Column("album", sa.String(), nullable=True),
        sa.Column("album_artist", sa.String(), nullable=True),
        sa.Column("compilation", sa.Boolean(), nullable=False),
        sa.Column("track_number", sa.Integer(), nullable=True),
        sa.Column("disc_number", sa.Integer(), nullable=True),
        sa.Column("track_count", sa.Integer(), nullable=True),
        sa.Column("disc_count", sa.Integer(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("play_count", sa.Integer(), nullable=True),
        sa.Column("skip_count", sa.Integer(), nullable=True),
        sa.Column("date_added", sa.DateTime(timezone=True), nullable=True),
        sa.Column("date_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("genre", sa.String(), nullable=True),
        sa.Column("audio_file_extension", sa.String(), nullable=True),
        sa.Column("is_purchased", sa.Boolean(), nullable=False),
        sa.Column("purchased_track_identifier", sa.BIGINT(), nullable=True),
        sa.Column("audio_matched_track_identifier", sa.BIGINT(), nullable=True),
        sa.ForeignKeyConstraint(["track_id"], ["track.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("track_identifier"),
    )
    op.create_table(
        "appletrack",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=True),
        sa.Column("apple_track_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("album", sa.String(), nullable=True),
        sa.Column("artist", sa.String(), nullable=True),
        sa.Column("album_artist", sa.String(), nullable=True),
        sa.Column("apple_music", sa.Boolean(), nullable=False),
        sa.Column("compilation", sa.Boolean(), nullable=True),
        sa.Column("track_number", sa.Integer(), nullable=True),
        sa.Column("disc_number", sa.Integer(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("release_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("loved", sa.Boolean(), nullable=False),
        sa.Column("favorited", sa.Boolean(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("rating_computed", sa.Boolean(), nullable=False),
        sa.Column("album_loved", sa.Boolean(), nullable=False),
        sa.Column("album_rating", sa.Integer(), nullable=True),
        sa.Column("album_rating_computed", sa.Boolean(), nullable=False),
        sa.Column("date_added", sa.DateTime(timezone=True), nullable=False),
        sa.Column("date_modified", sa.DateTime(timezone=True), nullable=False),
        sa.Column("play_date_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("play_date", sa.BIGINT(), nullable=True),
        sa.Column("total_time", sa.Integer(), nullable=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column(
            "track_type",
            sa.Enum("URL", "Remote", "File", name="tracktype", native_enum=False),
            nullable=False,
        ),
        sa.Column("persistent_id", sa.String(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("MPEG4_AUDIO", "AAC_BOUGHT", "MPEG_AUDIO", "AAC", "AAC_AM", "MPEG4_VIDEO", name="kind"),
            nullable=True,
        ),
        sa.Column("grouping", sa.String(), nullable=True),
        sa.Column("genre", sa.String(), nullable=True),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("bit_rate", sa.Integer(), nullable=True),
        sa.Column("sample_rate", sa.Integer(), nullable=True),
        sa.Column("bpm", sa.Integer(), nullable=True),
        sa.Column("normalization", sa.Integer(), nullable=True),
        sa.Column("volume_adjustment", sa.Integer(), nullable=True),
        sa.Column("play_count", sa.Integer(), nullable=True),
        sa.Column("skip_count", sa.Integer(), nullable=True),
        sa.Column("skip_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disliked", sa.Boolean(), nullable=False),
        sa.Column("comments", sa.String(), nullable=True),
        sa.Column("sort_name", sa.String(), nullable=True),
        sa.Column("sort_artist", sa.String(), nullable=True),
        sa.Column("sort_album_artist", sa.String(), nullable=True),
        sa.Column("sort_album", sa.String(), nullable=True),
        sa.Column("sort_composer", sa.String(), nullable=True),
        sa.Column("work", sa.String(), nullable=True),
        sa.Column("composer", sa.String(), nullable=True),
        sa.Column("movement_name", sa.String(), nullable=True),
        sa.Column("movement_count", sa.Integer(), nullable=True),
        sa.Column("disc_count", sa.Integer(), nullable=True),
        sa.Column("track_count", sa.Integer(), nullable=True),
        sa.Column("artwork_count", sa.Integer(), nullable=True),
        sa.Column("file_folder_count", sa.Integer(), nullable=True),
        sa.Column("library_folder_count", sa.Integer(), nullable=True),
        sa.Column("protected", sa.Boolean(), nullable=False),
        sa.Column("music_video", sa.Boolean(), nullable=False),
        sa.Column("has_video", sa.Boolean(), nullable=False),
        sa.Column("part_of_gapless_album", sa.Boolean(), nullable=True),
        sa.Column("playlist_only", sa.Boolean(), nullable=False),
        sa.Column("purchased", sa.Boolean(), nullable=False),
        sa.Column("matched", sa.Boolean(), nullable=False),
        sa.Column("explicit", sa.Boolean(), nullable=False),
        sa.Column("clean", sa.Boolean(), nullable=False),
        sa.Column("hd", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["track_id"], ["track.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("apple_track_id"),
        sa.UniqueConstraint("persistent_id"),
    )
    op.create_table(
        "apple_ms_playlist_track",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("playlist_id", sa.Integer(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["playlist_id"], ["apple_ms_playlist.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["track_id"], ["apple_ms_track.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_apple_ms_playlist_track_playlist_id"),
        "apple_ms_playlist_track",
        ["playlist_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_apple_ms_playlist_track_position"), "apple_ms_playlist_track", ["position"], unique=False
    )
    op.create_index(
        op.f("ix_apple_ms_playlist_track_track_id"), "apple_ms_playlist_track", ["track_id"], unique=False
    )
    op.create_table(
        "appleplaylisttrack",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("playlist_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["playlist_id"], ["appleplaylist.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["track_id"], ["appletrack.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_appleplaylisttrack_playlist_id"), "appleplaylisttrack", ["playlist_id"], unique=False
    )
    op.create_index(op.f("ix_appleplaylisttrack_position"), "appleplaylisttrack", ["position"], unique=False)
    op.create_index(op.f("ix_appleplaylisttrack_track_id"), "appleplaylisttrack", ["track_id"], unique=False)
