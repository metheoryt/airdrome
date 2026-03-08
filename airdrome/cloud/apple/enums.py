import enum


class TrackType(enum.StrEnum):
    URL = "URL"
    Remote = "Remote"
    File = "File"


class Kind(enum.StrEnum):
    MPEG4_AUDIO = "Аудиофайл MPEG-4"  # lossless m4a
    AAC_BOUGHT = "Купленное аудио AAC"  # iTunes Match track, downloaded from apple music, m4a
    MPEG_AUDIO = "Аудиофайл MPEG"  # mp3
    AAC = "Аудиофайл AAC"  # lossy m4a
    AAC_AM = "Аудиофайл AAC из Apple Music"  # Apple Music, DRM protected m4v
    MPEG4_VIDEO = "Видеофайл MPEG-4"  # mp4 video
