# import everything
airdrome import .\data\apple\AppleMusicLibrary.xml
airdrome import '.\data\apple\Apple Media Services information Part 1 of 2.zip'
airdrome import .\data\listenbrainz\listenbrainz_metheoryt_1774472711.zip
airdrome import C:\Users\methe\Music\PicardedMusic\

# after all data is collected,
# match tracks between each other, create canonical tracks and playlists
airdrome land -t 0.4 --merge-playlists

# deduplicate (manual choices are restored from, and mirrored back to, the library automatically)
airdrome dedup -s "artist,duration" -s "artist,year" -s "album_artist,duration" -c year --review  # batch, then manual review

# copy all files into configured directory. Main files separately, copies - separately.
airdrome organize

# $$$
# After that, start fresh Navidrome pointing to the same directory.
# Let it scan it.
# Note the database (navidrome.db) path.
# $$$

