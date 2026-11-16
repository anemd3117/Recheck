"""새 인용 정규화 원문이 공유용 진단 ZIP으로 새지 않게 한다."""

import zipfile

from recheck.storage import Store, new_id


def test_diagnostic_zip_omits_normalized_model_text_and_completion_bodies(tmp_path):
    store = Store(tmp_path)
    run = dict(
        run_id=new_id('run'),
        status='partial',
        settings={},
        llm={
            'status': 'success',
            'validated_text': 'private-observation-alpha',
            'citation_completions': [
                {
                    'normalized_text': 'private-observation-beta',
                    'additions': [{'reason': 'private-observation-gamma'}],
                }
            ],
        },
    )
    store.save(run)
    path = store.diagnostic_zip(run['run_id'], {})
    with zipfile.ZipFile(path) as archive:
        contents = b'\n'.join(archive.read(name) for name in archive.namelist()).decode('utf-8')
    assert 'private-observation-' not in contents
    assert 'success' in contents
