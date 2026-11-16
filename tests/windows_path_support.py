"""Exercise legacy Win32 path limits without changing the machine's registry."""

from contextlib import contextmanager
import io
import os


@contextmanager
def legacy_windows_io(monkeypatch):
    if os.name != 'nt':
        yield
        return

    def checked(path, limit):
        if isinstance(path, int):
            return
        text = os.fsdecode(path)
        if not text.startswith('\\\\?\\') and len(os.path.abspath(text)) >= limit:
            error = FileNotFoundError(2, 'Legacy Win32 path limit', text)
            error.winerror = 206
            raise error

    with monkeypatch.context() as patch:
        mkdir, open_file, stat, lstat, replace = os.mkdir, io.open, os.stat, os.lstat, os.replace

        def mkdir_limited(path, *args, **kwargs):
            checked(path, 248)
            return mkdir(path, *args, **kwargs)

        def open_limited(path, *args, **kwargs):
            checked(path, 260)
            return open_file(path, *args, **kwargs)

        def stat_limited(path, *args, **kwargs):
            checked(path, 260)
            return stat(path, *args, **kwargs)

        def lstat_limited(path, *args, **kwargs):
            checked(path, 260)
            return lstat(path, *args, **kwargs)

        def replace_limited(source, destination, *args, **kwargs):
            checked(source, 260)
            checked(destination, 260)
            return replace(source, destination, *args, **kwargs)

        patch.setattr(os, 'mkdir', mkdir_limited)
        patch.setattr(io, 'open', open_limited)
        patch.setattr(os, 'stat', stat_limited)
        patch.setattr(os, 'lstat', lstat_limited)
        patch.setattr(os, 'replace', replace_limited)
        yield
