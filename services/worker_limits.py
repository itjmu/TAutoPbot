"""Resource ceilings for untrusted media work; not a filesystem sandbox."""

import os

_job_handle = None


def windows_job_limits():
    """Bound the worker family and kill descendants when its last handle closes."""
    import ctypes
    from ctypes import wintypes

    global _job_handle
    if _job_handle is not None:
        return

    class Basic(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong),
            ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD),
            ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t),
            ("processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class IO(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "reads",
                "writes",
                "others",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", IO),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    info = Extended()
    info.basic.flags = 0x2000 | 0x8  # KILL_ON_JOB_CLOSE and ACTIVE_PROCESS
    info.basic.processes = 32
    memory = int(os.getenv("DOWNLOAD_WORKER_MEMORY_BYTES", "2147483648"))
    cpu = int(os.getenv("DOWNLOAD_WORKER_CPU_SECONDS", "1800"))
    if memory < 0 or cpu < 0:
        kernel.CloseHandle(handle)
        raise ValueError("Worker resource limits cannot be negative")
    if memory > 0:
        info.basic.flags |= 0x200
        info.job_memory = memory
    if cpu > 0:
        info.basic.flags |= 0x4
        info.basic.job_time = cpu * 10_000_000
    if not kernel.SetInformationJobObject(
        handle, 9, ctypes.byref(info), ctypes.sizeof(info)
    ) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise ctypes.WinError(error)
    _job_handle = handle


def apply_worker_limits():
    if os.name == "nt":
        windows_job_limits()
        return
    if os.name != "posix":
        return
    import resource

    limits = (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_CPU, int(os.getenv("DOWNLOAD_WORKER_CPU_SECONDS", "1800"))),
        (
            resource.RLIMIT_AS,
            int(os.getenv("DOWNLOAD_WORKER_MEMORY_BYTES", "2147483648")),
        ),
        (resource.RLIMIT_NOFILE, 256),
        (resource.RLIMIT_FSIZE, int(os.getenv("DOWNLOAD_SOURCE_BYTES", "1000000000"))),
    )
    for kind, value in limits:
        if value < 0:
            raise ValueError("Worker resource limits cannot be negative")
        if value == 0 and kind in {resource.RLIMIT_CPU, resource.RLIMIT_AS}:
            continue
        _, hard = resource.getrlimit(kind)
        cap = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(kind, (cap, cap))
