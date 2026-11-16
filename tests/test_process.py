import asyncio
import base64
import sys
import os
import threading
import time
import pytest
from recheck.process import run_process


def run(coro):
    return asyncio.run(coro)


def test_raw_bytes_and_eof():
    result = run(
        run_process(
            [
                sys.executable,
                "-c",
                "import sys; x=sys.stdin.buffer.read(); sys.stdout.buffer.write(x+b'\\xff'); sys.stderr.write('err')",
            ],
            stdin=b"abc",
        )
    )
    assert base64.b64decode(result["stdout_b64"]) == b"abc\xff"
    assert result["returncode"] == 0 and result["stderr"] == "err"


def test_timeout_drains_output():
    # The deadline includes interpreter/Windows job startup. Under concurrent
    # container load the first flushed byte takes 0.31-0.44s, so a 0.2s
    # deadline can correctly kill Python before there is any output to drain.
    # Give this fixture startup headroom; its 10s sleep must still be cut short.
    result = run(
        run_process(
            [sys.executable, "-u", "-c", "import time; print('before'); time.sleep(10)"],
            timeout=2,
        )
    )
    assert result["timed_out"] and "before" in result["stdout"]
    assert not result["cancelled"] and result["error"] is None
    assert not result["cleanup_errors"]
    assert result["duration"] < 5


def test_cancel():
    async def scenario():
        event = threading.Event()

        async def cancel():
            await asyncio.sleep(0.15)
            event.set()

        task = asyncio.create_task(cancel())
        result = await run_process(
            [sys.executable, "-c", "import time; time.sleep(10)"], cancel=event
        )
        await task
        return result

    assert run(scenario())["cancelled"]


def test_output_limit_terminates_and_caps():
    result = run(
        run_process(
            [sys.executable, "-u", "-c", "import sys; sys.stdout.write('x'*1000000)"],
            output_limit=1024,
        )
    )
    assert result["truncated"] and len(base64.b64decode(result["stdout_b64"])) <= 1024


def test_not_found_and_invalid_args():
    assert run(run_process(["recheck-no-such-command-476"]))["error"]
    with pytest.raises(ValueError):
        run(run_process(["hello\0"]))


@pytest.mark.skipif(os.name != "nt", reason="Windows batch launcher")
def test_windows_batch_path_with_spaces(tmp_path):
    folder = tmp_path / "한글 공백"
    folder.mkdir()
    script = folder / "launcher fixture.cmd"
    script.write_text("@echo off\r\necho VALUE [%~1]\r\n", encoding="ascii")
    result = run(run_process([str(script), "argument with spaces"]))
    assert result["returncode"] == 0, result
    assert "VALUE [argument with spaces]" in result["stdout"]


def test_timeout_terminates_owned_child_tree():
    code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(p.pid,flush=True); time.sleep(30)"
    result = run(run_process([sys.executable, "-u", "-c", code], timeout=2))
    assert result["timed_out"]
    _assert_owned_child_stopped(result)
    assert result["duration"] < 5, result
    assert not result["cleanup_errors"], result


def test_output_callback_failure_does_not_leave_process():
    def broken_callback(*args):
        raise RuntimeError("fixture UI sink unavailable")

    result = run(
        run_process(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('evidence',flush=True); time.sleep(30)",
            ],
            on_output=broken_callback,
        )
    )
    assert "fixture UI sink unavailable" in result["error"]
    assert result["returncode"] is None and result["duration"] < 5
    assert "evidence" in result["stdout"]


def _pipe_holding_child_command():
    return "import subprocess,sys,os; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(p.pid,flush=True); print(os.getpid(),flush=True)"


def _assert_owned_child_stopped(result):
    import psutil

    child = int(result['stdout'].splitlines()[0])
    try:
        process = psutil.Process(child)
    except psutil.NoSuchProcess:
        return

    # Windows job termination is asynchronous. Observe completion within a
    # bounded grace period, including disappearance between psutil queries.
    deadline = time.monotonic() + 3
    while True:
        try:
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.NoSuchProcess:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.01)

    # Clean up only this test's explicitly reported helper PID after failure.
    try:
        process.kill()
        process.wait(timeout=3)
    except psutil.NoSuchProcess:
        pass
    pytest.fail(f"Owned child {child} survived the cleanup grace period: {result!r}")


def test_timeout_after_parent_exits_still_stops_pipe_holding_child():
    result = run(
        run_process([sys.executable, '-u', '-c', _pipe_holding_child_command()], timeout=2)
    )
    _assert_owned_child_stopped(result)
    assert result['timed_out'] and result['duration'] < 5, result
    assert not result['cleanup_errors'], result


def test_cancel_after_parent_exits_still_stops_pipe_holding_child():
    async def scenario():
        import psutil

        event = threading.Event()
        received, tasks = bytearray(), []

        async def cancel_after_exit(pid):
            try:
                await asyncio.to_thread(psutil.Process(pid).wait, timeout=3)
            except psutil.NoSuchProcess:
                pass
            event.set()

        def on_output(channel, content):
            if channel != 'stdout':
                return
            received.extend(content)
            lines = received.splitlines()
            if len(lines) >= 2 and not tasks:
                # Arm cancellation after this fixture's parent really exits,
                # rather than assuming Python can start in less than 0.4s.
                tasks.append(asyncio.create_task(cancel_after_exit(int(lines[1]))))

        result = await run_process(
            [sys.executable, '-u', '-c', _pipe_holding_child_command()],
            cancel=event, timeout=10, on_output=on_output,
        )
        if tasks:
            await tasks[0]
        return result

    result = run(scenario())
    _assert_owned_child_stopped(result)
    assert result['cancelled'] and result['duration'] < 5, result
    assert not result['cleanup_errors'], result
