"""Windows shim cho chaos/kill_region.py. [CHI DUNG TREN WINDOWS — goi qua lab.ps1]

Windows khong co SIGSTOP/SIGCONT/SIGKILL, va os.kill(pid, 0) tren Windows lai
TerminateProcess (giet luon process) thay vi chi kiem tra con song. Shim nay thay
os.kill bang Win32 API tuong duong roi chay NGUYEN BAN chaos/kill_region.py,
nen chaos-events.jsonl van do chinh script goc ghi ra:

  sig 0       -> OpenProcess + GetExitCodeProcess == STILL_ACTIVE (chi kiem tra)
  SIGSTOP     -> NtSuspendProcess  (port van mo, TCP handshake xong, request TREO
                                    = dung hanh vi netblock nhu tren Linux)
  SIGCONT     -> NtResumeProcess
  SIGKILL     -> TerminateProcess  (cong dong -> ConnectError ngay)

    python scripts/win_chaos.py --region a --mode netblock --mock
    python scripts/win_chaos.py restore --region a --backend bare
"""
import ctypes
import errno
import os
import runpy
import signal
import sys
from ctypes import wintypes

if os.name == "nt":
    SIGKILL, SIGCONT, SIGSTOP = 9, 18, 19
    PROCESS_TERMINATE = 0x0001
    PROCESS_SUSPEND_RESUME = 0x0800
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

    def _open(pid: int, access: int):
        h = k32.OpenProcess(access, False, pid)
        if not h:
            raise ProcessLookupError(errno.ESRCH, f"khong mo duoc process {pid}")
        return h

    def win_kill(pid: int, sig: int):
        if sig == 0:
            h = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
            try:
                code = wintypes.DWORD()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != STILL_ACTIVE:
                    raise ProcessLookupError(errno.ESRCH, f"process {pid} da thoat")
            finally:
                k32.CloseHandle(h)
        elif sig in (SIGSTOP, SIGCONT):
            h = _open(pid, PROCESS_SUSPEND_RESUME)
            try:
                fn = ntdll.NtSuspendProcess if sig == SIGSTOP else ntdll.NtResumeProcess
                status = fn(h)
                if status != 0:
                    raise OSError(f"NtSuspend/ResumeProcess({pid}) NTSTATUS={status:#x}")
            finally:
                k32.CloseHandle(h)
        elif sig == SIGKILL:
            h = _open(pid, PROCESS_TERMINATE)
            try:
                if not k32.TerminateProcess(h, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                k32.CloseHandle(h)
        else:
            raise ValueError(f"signal {sig} khong ho tro tren Windows")

    signal.SIGKILL, signal.SIGCONT, signal.SIGSTOP = SIGKILL, SIGCONT, SIGSTOP
    os.kill = win_kill

if __name__ == "__main__":
    sys.path.insert(0, ".")
    sys.argv = ["chaos/kill_region.py", *sys.argv[1:]]
    runpy.run_path("chaos/kill_region.py", run_name="__main__")
