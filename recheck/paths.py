"""Windows filesystem paths, kept separate from external program arguments.

Only Python file operations should receive ``filesystem_path`` results. Docker,
cmd.exe and Java have their own path syntax and use ordinary checked paths.
"""

import os
from pathlib import Path
import re


def plain_local_path(value):
    """Unwrap an extended local drive path without disguising UNC/device paths."""
    path = Path(value)
    text = str(path)
    if os.name == 'nt' and re.match(r'^\\\\\?\\[A-Za-z]:\\', text):
        return Path(text[4:])
    return path


def filesystem_path(value):
    """Use the extended namespace for long absolute Windows file operations.

    Normalization precedes prefixing because extended paths do not interpret
    relative segments. The conservative threshold also covers directory APIs'
    MAX_PATH-minus-12 restriction. No machine policy is read or changed.
    """
    path = plain_local_path(value)
    if os.name != 'nt':
        return path
    path = Path(os.path.abspath(path))
    text = str(path)
    if len(text) < 240 or text.startswith(('\\\\?\\', '\\\\.\\')):
        return path
    if text.startswith('\\\\'):
        return Path('\\\\?\\UNC\\' + text[2:])
    return Path('\\\\?\\' + text)
