import itertools
from typing import TYPE_CHECKING

from ..utils import generate_path


class AppleFSDiscoverable:
    if TYPE_CHECKING:
        # provided by subclass (field or property); kept out of __annotations__ at runtime
        title: str
        album_artist: str
        artist: str
        album: str
        compilation: bool
        track_number: int | None
        disc_number: int | None

    @property
    def path_artist(self) -> str:
        if self.compilation:
            return "Compilations"
        return self.album_artist or self.artist or "Unknown Artist"

    @property
    def path_album(self) -> str:
        return self.album or "Unknown Album"

    def possible_locations(self, max_suffix: int = 1) -> list[str]:
        """
        Try different extensions since we can't rely on XML data to guess which extension to expect.
        Try MP3 first, with 40 chars name limit first (old convention that contains mostly original MP3s).
        """
        # duplicate track suffix, 0 means no suffix
        suffixes = list(range(max_suffix + 1))

        # filename length limit, 35 for newer iTunes/AM version, 40 for older
        name_limits = (40, 35)  # try old first, they mostly contain mp3

        # file extension
        extensions = ("mp3", "m4a")

        # whether to include a disc number in the filename
        disc_nums: list[int | None] = [None]
        if self.disc_number is not None:
            disc_nums.append(self.disc_number)

        paths = []
        # combine them into the cartesian product
        for sfx, lim, ext, disc_n in itertools.product(suffixes, name_limits, extensions, disc_nums):
            path = generate_path(
                artist=self.path_artist,
                album=self.path_album,
                title=self.title,
                ext=ext,
                track_n=self.track_number,
                disc_n=disc_n,
                suffix=sfx,
                name_limit=lim,
            )
            paths.append(path.as_posix())  # unix style to match TrackFile paths

        # deduplicate the list, preserving the order
        return list(dict.fromkeys(paths))
