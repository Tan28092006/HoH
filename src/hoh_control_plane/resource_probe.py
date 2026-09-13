"""Non-secret resource facts, using the standard library on the host and child."""
import os


def resource_snapshot():
    result = {"platform": os.name, "pid": os.getpid()}
    if os.name == "nt":
        import ctypes as c
        from ctypes import wintypes as w

        class Memory(c.Structure):
            _fields_ = [("length", w.DWORD), ("load", w.DWORD)] + [
                (name, c.c_ulonglong) for name in
                ("total", "available", "commit_total", "commit_available", "virtual_total", "virtual_available", "extended")]

        class Basic(c.Structure):
            _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64),
                        ("flags", w.DWORD), ("min_ws", c.c_size_t), ("max_ws", c.c_size_t),
                        ("active", w.DWORD), ("affinity", c.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ("r", "w", "o", "rb", "wb", "ob")]

        class Extended(c.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", c.c_size_t),
                        ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]

        kernel = c.WinDLL("kernel32", use_last_error=True)
        memory = Memory()
        memory.length = c.sizeof(memory)
        if kernel.GlobalMemoryStatusEx(c.byref(memory)):
            result.update(available_physical_bytes=memory.available, total_physical_bytes=memory.total,
                          available_commit_bytes=memory.commit_available, commit_limit_bytes=memory.commit_total,
                          available_virtual_bytes=memory.virtual_available, memory_load_percent=memory.load)
        else:
            result["memory_probe_error"] = c.get_last_error()
        limits = Extended()
        kernel.QueryInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p]
        kernel.QueryInformationJobObject.restype = w.BOOL
        if kernel.QueryInformationJobObject(None, 9, c.byref(limits), c.sizeof(limits), None):
            result["job_limits"] = {"flags": limits.basic.flags,
                "process_memory_bytes": limits.process_memory if limits.basic.flags & 0x100 else None,
                "job_memory_bytes": limits.job_memory if limits.basic.flags & 0x200 else None,
                "active_processes": limits.basic.active if limits.basic.flags & 8 else None,
                "peak_process_bytes": limits.peak_process, "peak_job_bytes": limits.peak_job,
                "scope": "queryable_current_job; ancestor job limits may be stricter"}
        else:
            result["job_limits"] = {"query_error": c.get_last_error(), "scope": "unavailable"}
    else:
        from pathlib import Path
        try:
            facts = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
            result["available_physical_bytes"] = int(facts["MemAvailable"].split()[0]) * 1024
        except (OSError, ValueError, KeyError):
            pass
        try:
            import resource
            result["address_space_limit"] = list(resource.getrlimit(resource.RLIMIT_AS))
        except (ImportError, OSError, ValueError):
            pass
        for name in ("memory.max", "memory.current"):
            try:
                result[name] = Path("/sys/fs/cgroup", name).read_text().strip()
            except OSError:
                pass
    return result
