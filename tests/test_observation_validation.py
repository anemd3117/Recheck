"""Malformed retained observations are unmeasured, never a matching empty pair."""
import base64
import copy

import pytest

from recheck.comparison import compare_behavior
from recheck.models import Settings


def observed(**changes):
    return dict(status='success', exit_code=0, stdout_b64='', stderr_b64='', **changes)


@pytest.mark.parametrize('field,value', [
    ('stdout_b64', None), ('stdout_b64', '%%%'), ('stdout_b64', 'A'),
    ('stdout_b64', b''), ('stderr_b64', 'not base64'),
    ('exit_code', '0'), ('exit_code', False), ('infrastructure_ok', 'true'),
    ('timed_out', 'false'),
])
def test_corrupt_success_observation_never_enters_match_denominator(field, value):
    broken = observed()
    broken[field] = value
    result = compare_behavior([dict(test_id='broken', original=broken,
                                    reimplementation=copy.deepcopy(broken))], Settings())
    assert result['denominator'] == result['matched'] == 0
    assert result['tests'][0]['execution_status'] == 'unmeasured'
    assert result['tests'][0]['match'] is None
    assert 'invalid_observation' in result['tests'][0]['observation_categories']


@pytest.mark.parametrize('field', ['stdout_b64', 'stderr_b64', 'exit_code'])
def test_missing_retained_observation_fields_are_not_valid_empty_output(field):
    broken = observed()
    broken.pop(field)
    result = compare_behavior([dict(test_id='missing', original=broken,
                                    reimplementation=observed())], Settings())
    assert result['denominator'] == 0 and result['tests'][0]['match'] is None
    assert result['category_counts']['invalid_observation'] == 1


def test_legacy_valid_empty_output_and_nonzero_exit_are_still_measured():
    empty = observed()
    error = observed()
    error['exit_code'] = 7
    result = compare_behavior([
        dict(test_id='empty', original=empty, reimplementation=copy.deepcopy(empty)),
        dict(test_id='error', original=error, reimplementation=copy.deepcopy(error)),
    ], Settings())
    assert result['denominator'] == result['matched'] == 2


def test_normalization_preserves_raw_binary_data_and_flags_stay_unmeasured():
    first = observed()
    first['stdout_b64'] = base64.b64encode(b'\xff\r\n').decode()
    second = observed()
    second['stdout_b64'] = base64.b64encode(b'\xff\n').decode()
    result = compare_behavior([dict(test_id='binary', original=first, reimplementation=second)],
                              Settings(normalize_newlines=True))
    assert result['matched'] == 1
    first['timed_out'] = True
    result = compare_behavior([dict(test_id='timeout', original=first, reimplementation=second)], Settings())
    assert result['denominator'] == 0
    assert result['tests'][0]['observation_categories'] == ['timed_out']
