"""Bounded subprocess output and process-tree lifetime ownership.

Windows uses a Job Object before releasing a trusted startup gate. POSIX uses
a process group and a pipe watchdog. This controls cooperative processes; it
does not provide filesystem/network isolation against hostile code.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


_GATE = r'''
import json, os, signal, subprocess, sys, threading
line = sys.stdin.readline()
if not line:
    sys.exit(125)
command = json.loads(line)
if os.name != 'nt':
    def watch_owner():
        sys.stdin.read()
        os.killpg(os.getpgrp(), signal.SIGKILL)
    threading.Thread(target=watch_owner, daemon=True).start()
try:
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, close_fds=True)
except OSError:
    sys.stderr.write('[hoh:exec-error] Command could not be started\n')
    sys.exit(127)
code = child.wait()
# The POSIX watchdog may still be reading stdin. Avoid interpreter shutdown
# trying to acquire its buffered-reader lock; the owner reaps the process group.
os._exit(code if 0 <= code <= 255 else 1)
'''


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False


class _WindowsJob:
    def __init__(self, process):
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64),
                        ('flags', w.DWORD), ('min_ws', c.c_size_t), ('max_ws', c.c_size_t),
                        ('active', w.DWORD), ('affinity', c.c_size_t),
                        ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(c.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', c.c_size_t),
                        ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.kernel.SetInformationJobObject.restype = w.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = w.BOOL
        self.kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.kernel.TerminateJobObject.restype = w.BOOL
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, no breakaway.
        try:
            if not self.kernel.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
                raise c.WinError(c.get_last_error())
            if not self.kernel.AssignProcessToJobObject(self.handle, w.HANDLE(int(process._handle))):
                raise c.WinError(c.get_last_error())
        except BaseException:
            self.close()
            raise

    def stop(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run_managed(command: list[str], *, cwd: str | Path, timeout: float,
                env: dict[str, str] | None = None, output_limit: int = 200_000,
                cancel=None) -> ProcessResult:
    if timeout <= 0 or output_limit < 0 or not command:
        raise ValueError('A command, positive timeout and nonnegative output limit are required')
    # -I -S prevents ambient sitecustomize/PYTHONPATH from running code before
    # the worker is attached to the Job Object / process-group watchdog.
    process = subprocess.Popen([sys.executable, '-I', '-S', '-u', '-c', _GATE],
        cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=os.name != 'nt', creationflags=0x08000000 if os.name == 'nt' else 0)
    job = None
    buffers = [bytearray(), bytearray()]
    truncated = [False, False]
    threads = []
    timed_out = cancelled = False
    def drain(stream, index):
        while chunk := stream.read(8192):
            remaining = max(0, output_limit - len(buffers[index]))
            buffers[index].extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated[index] = True
    def stop_tree():
        if job is not None:
            job.stop()
        elif os.name != 'nt':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()  # Startup gate was never released if job assignment failed.
    try:
        if os.name == 'nt':
            job = _WindowsJob(process)
        for index, stream in enumerate((process.stdout, process.stderr)):
            thread = threading.Thread(target=drain, args=(stream, index), daemon=True)
            thread.start()
            threads.append(thread)
        process.stdin.write((json.dumps(command) + '\n').encode())
        process.stdin.flush()
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if cancel is not None:
                try:
                    cancelled = bool(cancel())
                except Exception:
                    cancelled = True  # Loss of authority / DB connectivity stops work.
                if cancelled:
                    break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)
    finally:
        stop_tree()  # Also reap background children after a successful parent exit.
        process.wait(timeout=10)
        process.stdin.close()
        if job is not None:
            job.close()
        for thread in threads:
            thread.join(timeout=10)
        process.stdout.close()
        process.stderr.close()
    output = [data.decode('utf-8', errors='replace') + ('\n[output truncated]' if clipped else '')
              for data, clipped in zip(buffers, truncated)]
    if cancelled:
        output[1] += '\n[hoh:cancelled] Execution authority was revoked or unavailable.'
    return ProcessResult(process.returncode, *output, timed_out, cancelled)
