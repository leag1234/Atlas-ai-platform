def safe_path(value):
    from pathlib import PurePosixPath

    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts
