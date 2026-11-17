"""Atomic JSON writers must not share or abandon temporary files."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from recheck import models


def test_concurrent_writers_finish_without_sharing_their_temporary_file(tmp_path, monkeypatch):
    destination = tmp_path / 'result.json'
    first_ready = threading.Event()
    second_finished = threading.Event()
    replace = models.os.replace

    def interleaved_replace(source, target):
        if threading.current_thread().name.startswith('first-writer'):
            first_ready.set()
            if not second_finished.wait(10):
                raise TimeoutError('The second writer did not finish')
        return replace(source, target)

    monkeypatch.setattr(models.os, 'replace', interleaved_replace)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='first-writer') as pool:
        first = pool.submit(models.atomic_json, destination, {'writer': 'first', 'text': '한글'})
        try:
            assert first_ready.wait(10), 'The first writer did not reach atomic replacement'
            models.atomic_json(destination, {'writer': 'second', 'text': 'second payload'})
            assert json.loads(destination.read_text(encoding='utf-8')) == {
                'writer': 'second', 'text': 'second payload'
            }
        finally:
            second_finished.set()
        first.result(timeout=10)

    assert json.loads(destination.read_text(encoding='utf-8')) == {
        'writer': 'first', 'text': '한글'
    }
    assert set(tmp_path.iterdir()) == {destination}


def test_atomic_json_supports_a_filename_near_the_component_length_limit(tmp_path):
    destination = tmp_path / ('a' * 240 + '.json')

    models.atomic_json(destination, {'value': 42})

    assert json.loads(models.filesystem_path(destination).read_text(encoding='utf-8')) == {
        'value': 42
    }


@pytest.mark.parametrize('failure', ['serialization', 'replacement'])
def test_failed_writer_preserves_previous_file_and_removes_only_its_temporary(
    tmp_path, monkeypatch, failure
):
    destination = tmp_path / 'result.json'
    destination.write_text('{"previous": true}', encoding='utf-8')
    foreign_temporary = tmp_path / 'result.json.tmp-other-writer'
    foreign_temporary.write_bytes(b'owned by another writer')
    previous = destination.read_bytes()
    payload = {'next': 'value'}
    if failure == 'serialization':
        payload['cycle'] = payload
        error = ValueError
    else:
        def fail_replace(source, target):
            raise PermissionError('Destination is locked')

        monkeypatch.setattr(models.os, 'replace', fail_replace)
        error = PermissionError

    with pytest.raises(error):
        models.atomic_json(destination, payload)

    assert destination.read_bytes() == previous
    assert foreign_temporary.read_bytes() == b'owned by another writer'
    assert set(tmp_path.iterdir()) == {destination, foreign_temporary}
