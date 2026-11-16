"""Synthetic observed results with independent known match/mismatch/timeout outcomes."""
import base64


def observation(text, **extra):
    return dict(status='success', infrastructure_ok=True, exit_code=0,
                stdout_b64=base64.b64encode(text.encode()).decode(), stderr_b64='',
                timed_out=False, cancelled=False, truncated=False, oom_killed=False, **extra)


def issue_run():
    failed = observation('unfinished')
    failed.update(status='failed', timed_out=True, exit_code=None)
    return dict(run_id='run_' + 'a' * 32, attempt_id='attempt_original',
                analysis_mode='dynamic', ai_mode='off', mode='automatic', status='partial',
                files={side: dict(path=f'C:/private/{side}.bin', name=side+'.bin', sha256=char*64)
                       for side, char in [('original', 'a'), ('reimplementation', 'b')]},
                settings=dict(normalize_newlines=False, trim_trailing_whitespace=False),
                tests=[dict(id=identifier, args=[], stdin='local-sensitive-input')
                       for identifier in ('same', 'changed', 'timeout')],
                dynamic=[dict(test_id='same', original=observation('same'), reimplementation=observation('same')),
                         dict(test_id='changed', original=observation('old'), reimplementation=observation('<script>alert(1)</script>')),
                         dict(test_id='timeout', original=observation('done'), reimplementation=failed)],
                static={}, behavior={}, structure={}, evidence=[], diagnostics=[], events=[], llm={}, agent={},
                stages={'metadata': {'status': 'success'}, 'image': {'status': 'success'},
                        'dynamic': {'status': 'partial'}, 'llm': {'status': 'blocked', 'reason': 'not requested'}},
                analysis_status=dict(static='not_requested', dynamic='partial', ai='not_requested'))


def limited_static_run():
    functions = [dict(id=str(index), name='fn'+str(index), external=False,
                      decompile_status='success' if index < 100 else 'not_selected',
                      decompile='return 1;' if index < 100 else None,
                      blocks=[], edges=[], calls=[], mnemonics=['ret']) for index in range(130)]
    return dict(run_id='run_'+'c'*32, status='success', analysis_mode='static', ai_mode='off',
                tests=[], dynamic=[], settings={},
                static={'original': dict(total_functions=130, functions=functions, complete=True),
                        'reimplementation': dict(total_functions=130, functions=functions, complete=True)})
