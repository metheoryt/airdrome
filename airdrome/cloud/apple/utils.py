import os  # os.walk is useful for bottom-up traversal
from pathlib import Path


def remove_empty_dirs_recursively(root_dir_path: Path):
    """
    Removes all empty directories within the given root directory, recursively.

    Args:
        root_dir_path: The starting Path object from which to scan and remove
                       empty subdirectories.
    """
    if not root_dir_path.is_dir():
        print(f"Provided path is not a directory: {root_dir_path}")
        return

    # os.walk(topdown=False) traverses from the deepest directories upwards,
    # which is crucial for this operation.
    for dirpath, dirnames, filenames in os.walk(root_dir_path, topdown=False):
        # The current directory is empty if it contains no files and no subdirectories
        # that haven't already been deleted by previous iterations.
        if not dirnames and not filenames:
            try:
                # Use Path.rmdir() to attempt removal
                Path(dirpath).rmdir()
                print(f"Removed empty directory: {dirpath}")
            except OSError as e:
                # This might happen if another process creates a file, or for permissions issues
                print(f"Error removing directory {dirpath}: {e}")
            # except FileNotFoundError:
            #     # Sometimes a race condition might occur (?)
            #     pass


def ensure_truncated(s: str, maxlen: int = 35, is_filename: bool = False):
    if len(s) > maxlen:
        tr = s[:maxlen].rstrip()
    else:
        tr = s.rstrip()

    # quote windows path
    for char in '*<>:"/\\|?’“”':
        tr = tr.replace(char, "_")

    # quote leading/trailing dot
    if tr and tr[0] == ".":
        tr = "_" + tr[1:]

    if not is_filename and tr[-1] == ".":
        tr = tr[:-1] + "_"

    return tr


def generate_path(
    artist: str,
    album: str,
    title: str,
    ext: str,
    track_n: int | None,
    disc_n: int | None = None,
    suffix: int = 0,
    name_limit: int = 35,
) -> Path:
    """
    Generate an Apple Music library style relative track path.
    Example:
        Artist Name/Album Name/1-01 Track Name.mp3
    """
    artist_dir = ensure_truncated(artist, maxlen=name_limit)
    album_dir = ensure_truncated(album, maxlen=name_limit)
    filename = title
    if track_n is not None:
        tn = f"{track_n:02}"
        if disc_n is not None:
            tn = f"{disc_n}-{tn}"
        filename = f"{tn} {filename}"

    suffix_str = f" {suffix}" if suffix else ""
    suffix_ext = f"{suffix_str}.{ext}"
    filename_maxlen = name_limit - len(suffix_ext)
    filename = ensure_truncated(filename, maxlen=filename_maxlen, is_filename=True)
    filename = f"{filename}{suffix_ext}"
    return Path(artist_dir) / album_dir / filename
