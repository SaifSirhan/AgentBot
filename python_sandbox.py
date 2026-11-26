"""Fail-closed Windows AppContainer runner for execute_python."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import site
import struct
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from ctypes import wintypes
from dataclasses import dataclass, field
from functools import cache, lru_cache
from pathlib import Path


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


@dataclass
class SandboxResult:
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    error: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    app_container: bool = False
    job_controlled: bool = False
    file_snapshots: dict[str, dict | None] = field(default_factory=dict)


_RUN_LOCK = threading.Lock()
_PROCESS_MEMORY = 256 * 1024 * 1024
_JOB_MEMORY = 512 * 1024 * 1024
_PROCESS_LIMIT = 32
_ACL_TIMEOUT = 120
_ACL_WORKERS = 8
_SETUP_TIMEOUT = 120
_HOST_POLL = 0.1
_MUTEX_WAIT = 30
_ACL_VERSION = 2
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_OPEN_EXISTING = 3
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_KNOWN_FOLDER_IDS = {
    "Downloads": _GUID(0x374DE290, 0x123F, 0x4565,
                       (0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B)),
    "Documents": _GUID(0xFDD39AD0, 0x238F, 0x46AF,
                       (0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7)),
    "Desktop": _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                     (0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41)),
}

_JOB_KILL_ON_CLOSE = 0x2000
_JOB_ACTIVE_PROCESS = 0x0008
_JOB_PROCESS_MEMORY = 0x0100
_JOB_JOB_MEMORY = 0x0200
_SECURITY_CAPABILITIES = 0x00020009
_HANDLE_LIST = 0x00020002
_JOB_LIST = 0x0002000D
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_NO_WINDOW = 0x08000000
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 0x00000001
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_TOKEN_IS_APP_CONTAINER = 29
_TOKEN_INTEGRITY_LEVEL = 25
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_WAIT_ABANDONED = 128
_STILL_ACTIVE = 259
_ERROR_ALREADY_EXISTS = 183
_ERROR_BROKEN_PIPE = 109
_ERROR_INSUFFICIENT_BUFFER = 122
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_LABEL_SECURITY_INFORMATION = 0x00000010
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_ACCESS_ALLOWED_ACE = 0
_ACCESS_DENIED_ACE = 1
_SYSTEM_MANDATORY_LABEL_ACE_TYPE = 0x11
_ALL_APPLICATION_PACKAGES = "S-1-15-2-1"
_MEDIUM_INTEGRITY_SID = "S-1-16-8192"
_MEDIUM_INTEGRITY_RID = 0x2000
_SYSTEM_MANDATORY_LABEL_NO_WRITE_UP = 0x00000001
_ACL_REVISION = 2
_MAXDWORD = 0xFFFFFFFF
_RECEIPT_MAX_TARGETS = 64
_RECEIPT_MAX_REQUESTS = 256
_RECEIPT_MAX_LINE = 65536
_READ_ROOT_SCAN_LIMIT = 64
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_INHERITED_ACE = 0x10
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_GENERIC_EXECUTE = 0x20000000
_DELETE = 0x00010000
_WRITE_DENY_ACCESS = (
    _GENERIC_WRITE | _DELETE | _WRITE_DAC | 0x00080000 | 0x00000040)
_FILE_READ_EXECUTE_ACCESS = (
    0x00000001 | 0x00000008 | 0x00000020 | 0x00000080)
_FILE_WRITE_DENY_ACCESS = (
    _DELETE | _WRITE_DAC | 0x00080000 | 0x00000040
    | 0x00000002 | 0x00000004 | 0x00000010 | 0x00000100)
_MODIFY_ACCESS = 0x001301BF
_EXCESS_MODIFY_ACCESS = (
    _WRITE_DAC | 0x00080000 | 0x00000040 | _GENERIC_WRITE | 0x10000000)
_LMEM_FIXED = 0x0000
_DACL_PROTECTED = 0x1000
_WRITE_ACCESS = (
    0x10000000 | 0x02000000 | 0x40000000
    | 0x00000002 | 0x00000004 | 0x00000010 | 0x00000040
    | 0x00000100 | 0x00010000 | 0x00040000 | 0x00080000
)

_RUNNER = """import json
import msvcrt
import os
import site
import sys

code_path, project_dir, run_dir, receipt_write, receipt_read, *site_dirs = sys.argv[1:]
home = os.path.join(run_dir, "home")
os.makedirs(home, exist_ok=True)
drive, home_path = os.path.splitdrive(home)
os.environ.update({
    "HOME": home,
    "USERPROFILE": home,
    "TEMP": home,
    "TMP": home,
    "LOCALAPPDATA": home,
    "APPDATA": home,
    "HOMEDRIVE": drive,
    "HOMEPATH": home_path or "\\\\",
})
site.ENABLE_USER_SITE = False
sys.dont_write_bytecode = True
for directory in site_dirs:
    site.addsitedir(directory)
if project_dir not in sys.path:
    sys.path.insert(0, project_dir)
os.chdir(project_dir)
sys.argv = ["-c"]
receipt_write_fd = msvcrt.open_osfhandle(int(receipt_write), os.O_WRONLY)
receipt_read_fd = msvcrt.open_osfhandle(int(receipt_read), os.O_RDONLY)
_write_receipt = os.write
_read_receipt = os.read

def _request_file_snapshot(path):
    if isinstance(path, int):
        return
    try:
        value = os.fsdecode(path)
    except TypeError:
        return
    if not value or "\\x00" in value:
        return
    message = json.dumps({"path": value}, ensure_ascii=True, separators=(",", ":"))
    try:
        _write_receipt(receipt_write_fd, (message + "\\n").encode("ascii"))
        status = _read_receipt(receipt_read_fd, 1)
    except OSError as exc:
        raise RuntimeError("sandbox mutation tracking is unavailable") from exc
    if status == b"0":
        raise RuntimeError("sandbox cannot track this file mutation")
    return status == b"1"

def _audit_file_mutation(event, args):
    if event == "open":
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        write_flags = (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT
                       | os.O_TRUNC | os.O_EXCL)
        mode_writes = isinstance(mode, str) and any(flag in mode for flag in "wax+")
        if isinstance(flags, int):
            if not flags & write_flags and not mode_writes:
                return
        elif not mode_writes:
            return
        _request_file_snapshot(args[0])
        return
    if event in {
        "os.remove", "os.unlink", "os.rename", "os.replace", "os.link",
        "os.symlink", "os.mkdir", "os.rmdir", "shutil.copyfile",
        "shutil.copymode", "shutil.copystat", "shutil.rmtree", "sqlite3.connect",
    }:
        for value in args:
            if isinstance(value, (str, bytes, os.PathLike)):
                _request_file_snapshot(value)

with open(code_path, "r", encoding="utf-8", errors="surrogatepass") as handle:
    source = handle.read()
namespace = {
    "__name__": "__main__",
    "__file__": "<string>",
    "__package__": None,
    "__spec__": None,
    "__cached__": None,
    "__builtins__": __builtins__,
}
sys.addaudithook(_audit_file_mutation)
exec(compile(source, "<string>", "exec"), namespace, namespace)
"""


class _SIDAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SIDAndAttributes)]


class _SecurityCapabilities(ctypes.Structure):
    _fields_ = [
        ("AppContainerProfile", ctypes.c_void_p),
        ("Capabilities", ctypes.POINTER(_SIDAndAttributes)),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("AttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SIDAndAttributes)]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


@lru_cache(maxsize=1)
def _apis():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)

    kernel.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_void_p, wintypes.DWORD]
    kernel.CreatePipe.restype = wintypes.BOOL
    kernel.SetHandleInformation.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel.SetHandleInformation.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    kernel.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    kernel.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(_StartupInfo), ctypes.POINTER(_ProcessInformation)]
    kernel.CreateProcessW.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.IsProcessInJob.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
        ctypes.c_void_p]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL

    userenv.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR, ctypes.c_void_p]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    advapi.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi.FreeSid.argtypes = [ctypes.c_void_p]
    advapi.FreeSid.restype = ctypes.c_void_p
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityInfo.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p]
    advapi.GetSecurityInfo.restype = wintypes.DWORD
    advapi.SetSecurityInfo.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    advapi.SetSecurityInfo.restype = wintypes.DWORD
    advapi.InitializeAcl.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    advapi.InitializeAcl.restype = wintypes.BOOL
    advapi.AddAce.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD]
    advapi.AddAce.restype = wintypes.BOOL
    advapi.AddAccessAllowedAceEx.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_void_p]
    advapi.AddAccessAllowedAceEx.restype = wintypes.BOOL
    advapi.AddAccessDeniedAceEx.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_void_p]
    advapi.AddAccessDeniedAceEx.restype = wintypes.BOOL
    advapi.AddMandatoryAce.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_void_p]
    advapi.AddMandatoryAce.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    kernel.LocalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel.LocalAlloc.restype = ctypes.c_void_p
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    return kernel, advapi, userenv


def _close(handle):
    if handle:
        _apis()[0].CloseHandle(handle)


def _open_reparse_point(path, access):
    kernel = _apis()[0]
    handle = kernel.CreateFileW(
        path, access,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None, _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise _win_error(f"CreateFileW({path})")
    return handle


def _win_error(action):
    return OSError(f"{action} failed (Windows error {ctypes.get_last_error()}).")


def _hresult_error(action, code):
    return OSError(f"{action} failed (HRESULT 0x{code & 0xffffffff:08x}).")


@cache
def _known_folder(name):
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    shell32.SHGetKnownFolderPath.restype = wintypes.LONG
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    path = wintypes.LPWSTR()
    code = shell32.SHGetKnownFolderPath(
        ctypes.byref(_KNOWN_FOLDER_IDS[name]), 0, None, ctypes.byref(path))
    if code < 0:
        raise _hresult_error(f"SHGetKnownFolderPath({name})", code)
    try:
        return path.value
    finally:
        ole32.CoTaskMemFree(ctypes.cast(path, ctypes.c_void_p))


def _sid_string(sid):
    kernel, advapi, _ = _apis()
    text = ctypes.c_wchar_p()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise _win_error("ConvertSidToStringSidW")
    try:
        return text.value
    finally:
        kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _current_user_sid():
    kernel, advapi, _ = _apis()
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(
            kernel.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _win_error("OpenProcessToken")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
            raise _win_error("GetTokenInformation(TokenUser)")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(
                token, _TOKEN_USER, buffer, size.value, ctypes.byref(size)):
            raise _win_error("GetTokenInformation(TokenUser)")
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        return _sid_string(user.User.Sid)
    finally:
        _close(token)


def _profile_identity():
    user_sid = _current_user_sid()
    local = os.path.abspath(os.path.expanduser(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")))
    identity = f"{user_sid}\0{os.path.normcase(local)}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    name = f"Kestrel.PythonSandbox.{digest}"
    state_path = os.path.join(local, "Kestrel", f"python_sandbox_{digest}.json")
    return name, user_sid, state_path


def _ensure_profile(name, state_path=None):
    _, advapi, userenv = _apis()
    sid = ctypes.c_void_p()
    code = userenv.CreateAppContainerProfile(
        name, "Kestrel", "Isolated execute_python", None, 0, ctypes.byref(sid))
    if code == 0:
        try:
            return _sid_string(sid)
        finally:
            advapi.FreeSid(sid)
    if code & 0xffffffff != (0x80070000 | _ERROR_ALREADY_EXISTS):
        raise _hresult_error("CreateAppContainerProfile", code)
    if state_path is not None and not os.path.isfile(state_path):
        raise OSError(
            "The existing Python sandbox profile has no ACL state; refusing to reuse it.")
    code = userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
    if code != 0:
        raise _hresult_error("DeriveAppContainerSidFromAppContainerName", code)
    try:
        return _sid_string(sid)
    finally:
        advapi.FreeSid(sid)


def _is_nested_path(path, parent):
    try:
        return os.path.commonpath([path, parent]) == parent
    except ValueError:
        return False


def _collapse_roots(paths):
    normalized = sorted(
        {os.path.normcase(os.fspath(path)) for path in paths},
        key=lambda path: (len(path), path))
    kept = []
    for path in normalized:
        try:
            nested = any(
                os.path.commonpath([path, parent]) == parent for parent in kept)
        except ValueError:
            nested = False
        if not nested:
            kept.append(path)
    return kept


def _normalize_roots(paths):
    normalized = []
    for value in paths:
        path = os.path.realpath(os.path.abspath(os.path.expandvars(
            os.path.expanduser(os.fspath(value)))))
        if not os.path.isdir(path):
            raise ValueError(f"Sandbox root is not an existing directory: {path}")
        normalized.append(os.path.normcase(path))
    return sorted(set(normalized))


def _default_roots():
    home = Path(os.environ.get("USERPROFILE", Path.home()))
    candidates = [Path(__file__).resolve().parent]
    for name in ("Downloads", "Documents", "Desktop"):
        try:
            folder = _known_folder(name)
        except OSError:
            folder = home / name
        candidates.append(Path(folder))
    return [str(path) for path in candidates if path and Path(path).is_dir()]


def _resolve_interpreter():
    executable = Path(sys.executable)
    if not re.fullmatch(
            r"python(?:\d+(?:\.\d+)*)?\.exe", executable.name, re.IGNORECASE):
        raise ValueError(
            "execute_python requires a real Python interpreter; "
            "a frozen or pythonw launcher cannot be sandboxed safely.")
    if not executable.is_file():
        raise ValueError(f"The Python interpreter is missing: {executable}")
    return str(executable.resolve())


def _read_roots(cwd, interpreter):
    roots = [cwd, str(Path(interpreter).parent)]
    roots.extend(path for path in site.getsitepackages() if os.path.isdir(path))
    return _normalize_roots(roots)


def _check_host_work(stop_event=None, deadline=None):
    if stop_event is not None and stop_event.is_set():
        raise InterruptedError("Sandbox setup cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("Python sandbox setup exceeded its time limit.")


def _run_icacls(path, arguments, *, stop_event=None, deadline=None):
    _check_host_work(stop_event, deadline)
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    executable = os.path.join(system_root, "System32", "icacls.exe")
    command_deadline = time.monotonic() + _ACL_TIMEOUT
    if deadline is not None:
        command_deadline = min(command_deadline, deadline)
    process = subprocess.Popen(
        [executable, path, *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={"SystemRoot": system_root},
    )
    try:
        while True:
            _check_host_work(stop_event, command_deadline)
            try:
                process.communicate(timeout=_HOST_POLL)
                return process.returncode == 0
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def _is_reparse_point(path):
    return bool(os.lstat(path).st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _read_root_acls(path, access, *, include_label=False):
    path = os.fspath(path)
    kernel, advapi, _ = _apis()
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    sacl = ctypes.c_void_p()
    handle = None
    security_info = _DACL_SECURITY_INFORMATION
    if include_label:
        security_info |= _LABEL_SECURITY_INFORMATION
    if _is_reparse_point(path):
        handle = _open_reparse_point(path, access)
        code = advapi.GetSecurityInfo(
            handle, _SE_FILE_OBJECT, security_info,
            None, None, ctypes.byref(dacl), ctypes.byref(sacl),
            ctypes.byref(descriptor))
        action = f"GetSecurityInfo({path})"
    else:
        code = advapi.GetNamedSecurityInfoW(
            path, _SE_FILE_OBJECT, security_info,
            None, None, ctypes.byref(dacl), ctypes.byref(sacl),
            ctypes.byref(descriptor))
        action = f"GetNamedSecurityInfoW({path})"
    if code:
        if descriptor:
            kernel.LocalFree(descriptor)
        _close(handle)
        raise _security_error(action, code)
    return kernel, advapi, descriptor, dacl, sacl, handle


def _read_root_dacl(path, access):
    kernel, advapi, descriptor, dacl, _, handle = _read_root_acls(path, access)
    return kernel, advapi, descriptor, dacl, handle


def _dacl_entries(dacl, path):
    if not dacl.value:
        raise OSError(f"Cannot update a path without a DACL: {path}")
    header = ctypes.string_at(dacl, 8)
    revision = header[0]
    if revision not in (2, 3, 4):
        raise OSError(f"Unsupported DACL revision for {path}: {revision}")
    acl_size = struct.unpack_from("<H", header, 2)[0]
    ace_count = struct.unpack_from("<H", header, 4)[0]
    if acl_size < 8:
        raise OSError(f"Malformed DACL for {path}")
    raw = ctypes.string_at(dacl, acl_size)
    entries = []
    offset = 8
    for _ in range(ace_count):
        if offset + 4 > acl_size:
            raise OSError(f"Malformed DACL for {path}")
        ace_size = struct.unpack_from("<H", raw, offset + 2)[0]
        if ace_size < 4 or offset + ace_size > acl_size:
            raise OSError(f"Malformed ACE for {path}")
        ace_type = raw[offset]
        ace_sid = None
        flags = None
        mask = None
        if ace_type in (
                _ACCESS_ALLOWED_ACE, _ACCESS_DENIED_ACE,
                _SYSTEM_MANDATORY_LABEL_ACE_TYPE):
            if ace_size < 12:
                raise OSError(f"Malformed access ACE for {path}")
            ace_sid = _sid_string(dacl.value + offset + 8)
            flags = raw[offset + 1]
            mask = struct.unpack_from("<I", raw, offset + 4)[0]
        entries.append((raw[offset:offset + ace_size], ace_sid, ace_type, flags, mask))
        offset += ace_size
    if offset != acl_size:
        raise OSError(f"Malformed DACL size for {path}")
    return revision, entries


def _package_group_access_is_safe(entries, access_mask):
    forbidden = (_WRITE_ACCESS if access_mask == _GENERIC_READ | _GENERIC_EXECUTE
                 else _EXCESS_MODIFY_ACCESS)
    return not any(
        ace_sid == _ALL_APPLICATION_PACKAGES
        and ace_type == _ACCESS_ALLOWED_ACE and mask & forbidden
        for _, ace_sid, ace_type, _, mask in entries)


def _scan_read_root(root, sid, *, stop_event=None, deadline=None):
    root = os.path.normcase(os.path.abspath(os.fspath(root)))
    findings = []

    def walk_error(error):
        raise error

    def paths():
        yield root
        for current, directories, files in os.walk(
                root, followlinks=False, onerror=walk_error):
            directories[:] = [
                name for name in directories
                if not _is_reparse_point(os.path.join(current, name))]
            for name in [*directories, *files]:
                yield os.path.normcase(os.path.join(current, name))

    for path in paths():
        _check_host_work(stop_event, deadline)
        if not os.path.lexists(path):
            continue
        try:
            kernel, _, descriptor, dacl, _, handle = _read_root_acls(
                path, _READ_CONTROL)
        except OSError:
            if not os.path.lexists(path):
                continue
            raise
        try:
            _, entries = _dacl_entries(dacl, path)
        finally:
            if descriptor:
                kernel.LocalFree(descriptor)
            _close(handle)
        kind = None
        if any(
                ace_sid == _ALL_APPLICATION_PACKAGES
                and ace_type == _ACCESS_ALLOWED_ACE and mask & _WRITE_ACCESS
                for _, ace_sid, ace_type, _, mask in entries):
            kind = "group"
        elif any(
                ace_sid == sid and ace_type == _ACCESS_ALLOWED_ACE
                and not flags & _INHERITED_ACE and mask & _WRITE_ACCESS
                for _, ace_sid, ace_type, flags, mask in entries):
            kind = "write"
        if kind:
            findings.append({"path": path, "kind": kind})
            if len(findings) > _READ_ROOT_SCAN_LIMIT:
                raise OSError(
                    f"Too many unsafe Python sandbox read-root entries: {root}")
    _check_host_work(stop_event, deadline)
    return findings


def _start_read_root_scans(roots, sid, *, stop_event=None, deadline=None):
    scans = []
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    environment = {
        "SystemRoot": system_root,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }
    try:
        for root in roots:
            _check_host_work(stop_event, deadline)
            process = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__),
                 "--scan-read-root", root, sid],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                creationflags=_CREATE_NO_WINDOW,
            )
            scans.append((root, process))
    except Exception:
        _stop_read_root_scans(scans)
        raise
    return scans


def _stop_read_root_scans(scans):
    for _, process in scans:
        if process.poll() is None:
            process.kill()
    for _, process in scans:
        process.communicate()


def _finish_read_root_scans(scans, *, stop_event=None, deadline=None):
    findings = []
    for root, process in scans:
        while True:
            _check_host_work(stop_event, deadline)
            try:
                stdout, stderr = process.communicate(timeout=_HOST_POLL)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            detail = stderr.strip()[:500]
            raise OSError(
                f"Cannot audit Python sandbox read root {root}: {detail}")
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise OSError(
                f"Invalid Python sandbox read-root audit for {root}") from exc
        if not isinstance(result, list):
            raise OSError(f"Invalid Python sandbox read-root audit for {root}")
        for item in result:
            if (not isinstance(item, dict)
                    or not isinstance(item.get("path"), str)
                    or item.get("kind") not in ("write", "group")):
                raise OSError(
                    f"Invalid Python sandbox read-root finding for {root}")
            findings.append(item)
    return findings


def _scan_worker_main():
    if len(sys.argv) != 4 or sys.argv[1] != "--scan-read-root":
        return
    findings = _scan_read_root(sys.argv[2], sys.argv[3])
    json.dump(findings, sys.stdout, ensure_ascii=True, separators=(",", ":"))


def _mandatory_label_state(entries, *, is_directory=True):
    labels = [
        (sid, flags, mask)
        for _, sid, ace_type, flags, mask in entries
        if ace_type == _SYSTEM_MANDATORY_LABEL_ACE_TYPE]
    if not labels:
        return "none"
    if len(labels) != 1:
        return "unsafe"
    sid, flags, mask = labels[0]
    inheritable = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE
    if sid != _MEDIUM_INTEGRITY_SID or mask != _SYSTEM_MANDATORY_LABEL_NO_WRITE_UP:
        return "unsafe"
    if flags & _INHERITED_ACE:
        return "inherited"
    if flags & inheritable != (inheritable if is_directory else 0):
        return "unsafe"
    return "direct"


def _root_label_state(path, *, stop_event=None, deadline=None):
    _check_host_work(stop_event, deadline)
    if not os.path.lexists(path):
        return None
    kernel, _, descriptor, _, sacl, handle = _read_root_acls(
        path, _READ_CONTROL, include_label=True)
    try:
        if not sacl.value:
            return "none"
        _, entries = _dacl_entries(sacl, path)
        return _mandatory_label_state(entries, is_directory=os.path.isdir(path))
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        _close(handle)
        _check_host_work(stop_event, deadline)


def _root_access_matches(path, sid, access_mask, *, stop_event=None, deadline=None):
    _check_host_work(stop_event, deadline)
    if not os.path.lexists(path):
        return False
    kernel, _, descriptor, dacl, sacl, handle = _read_root_acls(
        path, _READ_CONTROL, include_label=True)
    try:
        _, entries = _dacl_entries(dacl, path)
        if sacl.value:
            _, label_entries = _dacl_entries(sacl, path)
        else:
            label_entries = []
        if not _package_group_access_is_safe(entries, access_mask):
            return False
        read_only = access_mask == _GENERIC_READ | _GENERIC_EXECUTE
        is_directory = os.path.isdir(path)
        if read_only and _mandatory_label_state(
                label_entries, is_directory=is_directory) not in (
                    "direct", "inherited"):
            return False
        if not read_only and label_entries:
            return False
        direct = [
            (ace_type, flags, mask)
            for _, ace_sid, ace_type, flags, mask in entries
            if ace_sid == sid and not flags & _INHERITED_ACE]
        allows = [entry for entry in direct if entry[0] == _ACCESS_ALLOWED_ACE]
        denies = [entry for entry in direct if entry[0] == _ACCESS_DENIED_ACE]
        inheritable = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE
        if is_directory:
            if not any(
                    flags & inheritable == inheritable
                    and mask & access_mask == access_mask
                    for _, flags, mask in allows):
                return False
        else:
            file_access = _FILE_READ_EXECUTE_ACCESS if read_only else access_mask
            if not any(
                    mask & file_access == file_access for _, _, mask in allows):
                return False
        if read_only:
            if any(mask & _WRITE_ACCESS for _, _, mask in allows):
                return False
            if is_directory:
                if not any(
                        flags & inheritable == inheritable
                        and mask == _WRITE_DENY_ACCESS
                        for _, flags, mask in denies):
                    return False
            else:
                if not any(
                        mask & _FILE_WRITE_DENY_ACCESS == _FILE_WRITE_DENY_ACCESS
                        for _, _, mask in denies):
                    return False
            read_rights = (
                _GENERIC_READ | _GENERIC_EXECUTE
                | 0x00000001 | 0x00000008 | 0x00000020 | 0x00000080)
            if any(mask & read_rights for _, _, mask in denies):
                return False
        else:
            if denies or any(mask & _EXCESS_MODIFY_ACCESS for _, _, mask in allows):
                return False
        return True
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        _close(handle)
        _check_host_work(stop_event, deadline)


def _set_root_access(path, sid, access_mask, *, stop_event=None, deadline=None,
                     clear_label=True):
    path = os.fspath(path)
    _check_host_work(stop_event, deadline)
    kernel, advapi, descriptor, dacl, sacl, handle = _read_root_acls(
        path, _READ_CONTROL | _WRITE_DAC, include_label=True)
    package_sid = ctypes.c_void_p()
    label_sid = ctypes.c_void_p()
    new_acl = ctypes.c_void_p()
    new_sacl = ctypes.c_void_p()
    try:
        revision, entries = _dacl_entries(dacl, path)
        if sacl.value:
            _, label_entries = _dacl_entries(sacl, path)
        else:
            label_entries = []
        label_state = _mandatory_label_state(
            label_entries, is_directory=os.path.isdir(path))
        if label_state == "unsafe":
            raise OSError(
                f"Unsafe mandatory label for Python sandbox root: {path}")
        if not _package_group_access_is_safe(entries, access_mask):
            raise OSError(
                f"Unsafe ALL_APPLICATION_PACKAGES access for Python sandbox root: {path}")
        read_only = access_mask == _GENERIC_READ | _GENERIC_EXECUTE
        if access_mask is not None and not read_only and label_entries:
            raise OSError(
                f"A mandatory label blocks writable Python sandbox root: {path}")
        kept = [ace for ace, ace_sid, _, _, _ in entries if ace_sid != sid]
        clear_direct_label = (
            access_mask is None and clear_label and label_state == "direct")
        if access_mask is None and len(kept) == len(entries) and not clear_direct_label:
            return True
        new_size = 8 + sum(map(len, kept))
        if access_mask is not None:
            new_size += 256
        if new_size > 0xFFFF:
            raise OSError(f"DACL is too large to update: {path}")
        new_acl = kernel.LocalAlloc(_LMEM_FIXED, new_size)
        if not new_acl:
            raise _win_error("LocalAlloc")
        if not advapi.InitializeAcl(new_acl, new_size, revision):
            raise _win_error("InitializeAcl")
        for ace in kept:
            buffer = ctypes.create_string_buffer(ace)
            if not advapi.AddAce(
                    new_acl, revision, _MAXDWORD, buffer, len(ace)):
                raise _win_error("AddAce")
        if access_mask is not None:
            if not advapi.ConvertStringSidToSidW(sid, ctypes.byref(package_sid)):
                raise _win_error("ConvertStringSidToSidW")
            flags = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE
            if read_only:
                if not advapi.AddAccessDeniedAceEx(
                        new_acl, revision, flags, _WRITE_DENY_ACCESS, package_sid):
                    raise _win_error("AddAccessDeniedAceEx")
            if not advapi.AddAccessAllowedAceEx(
                    new_acl, revision, flags, access_mask, package_sid):
                raise _win_error("AddAccessAllowedAceEx")
        security_info = _DACL_SECURITY_INFORMATION
        if (read_only and label_state == "none") or clear_direct_label:
            new_sacl = kernel.LocalAlloc(_LMEM_FIXED, 256)
            if not new_sacl:
                raise _win_error("LocalAlloc")
            if not advapi.InitializeAcl(new_sacl, 256, _ACL_REVISION):
                raise _win_error("InitializeAcl(SACL)")
            if not clear_direct_label:
                if not advapi.ConvertStringSidToSidW(
                        _MEDIUM_INTEGRITY_SID, ctypes.byref(label_sid)):
                    raise _win_error("ConvertStringSidToSidW(Medium)")
                flags = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE
                if not advapi.AddMandatoryAce(
                        new_sacl, _ACL_REVISION, flags,
                        _SYSTEM_MANDATORY_LABEL_NO_WRITE_UP, label_sid):
                    raise _win_error("AddMandatoryAce")
            security_info |= _LABEL_SECURITY_INFORMATION
        _check_host_work(stop_event, deadline)
        if handle:
            code = advapi.SetSecurityInfo(
                handle, _SE_FILE_OBJECT, security_info,
                None, None, new_acl, new_sacl)
            action = f"SetSecurityInfo({path})"
        else:
            name = ctypes.create_unicode_buffer(path)
            code = advapi.SetNamedSecurityInfoW(
                name, _SE_FILE_OBJECT, security_info,
                None, None, new_acl, new_sacl)
            action = f"SetNamedSecurityInfoW({path})"
        if code:
            raise _security_error(action, code)
        _check_host_work(stop_event, deadline)
        return True
    finally:
        if label_sid:
            kernel.LocalFree(label_sid)
        if package_sid:
            kernel.LocalFree(package_sid)
        if new_sacl:
            kernel.LocalFree(new_sacl)
        if new_acl:
            kernel.LocalFree(new_acl)
        if descriptor:
            kernel.LocalFree(descriptor)
        _close(handle)


def _revoke_access(path, sid, *, stop_event=None, deadline=None, clear_label=True):
    if not os.path.lexists(path):
        return True
    return _set_root_access(
        path, sid, None, stop_event=stop_event, deadline=deadline,
        clear_label=clear_label)


def _apply_access_changes(changes, sid, *, stop_event=None, deadline=None,
                          clear_label_paths=None):
    if not changes:
        return
    clear_label_paths = set(clear_label_paths or ())
    remaining = set(changes)
    running = {}

    def apply(path):
        access = changes[path]
        if access is None:
            changed = _revoke_access(
                path, sid, stop_event=stop_event, deadline=deadline,
                clear_label=path in clear_label_paths)
        else:
            changed = _set_root_access(
                path, sid, access,
                stop_event=stop_event, deadline=deadline)
        if not changed:
            raise OSError(f"Cannot update Python sandbox access: {path}")
        return path

    with ThreadPoolExecutor(max_workers=min(len(changes), _ACL_WORKERS)) as executor:
        while remaining or running:
            _check_host_work(stop_event, deadline)
            for path in sorted(remaining):
                if len(running) >= _ACL_WORKERS:
                    break
                unfinished = remaining | set(running.values())
                has_parent = any(
                    parent != path and _is_nested_path(path, parent)
                    for parent in unfinished)
                if not has_parent:
                    running[executor.submit(apply, path)] = path
                    remaining.remove(path)
            if not running:
                raise OSError("Cannot order Python sandbox ACL updates.")
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                running.pop(future)
                future.result()


def _empty_state(sid):
    return {
        "acl_version": _ACL_VERSION,
        "sid": sid,
        "writable_roots": [],
        "read_roots": [],
        "pending_writable_roots": [],
        "pending_read_roots": [],
        "read_label_roots": [],
        "pending_read_label_roots": [],
        "protected_paths": {},
    }


def _load_state(path, sid):
    if not os.path.exists(path):
        return _empty_state(sid)
    with open(path, "r", encoding="utf-8") as handle:
        state = json.load(handle)
    if state.get("sid") != sid:
        raise ValueError("The Python sandbox ACL state belongs to another profile.")
    version = state.get("acl_version", 1)
    if version not in (1, _ACL_VERSION):
        raise ValueError("The Python sandbox ACL state version is unsupported.")
    state["acl_version"] = version
    for key in ("writable_roots", "read_roots", "pending_writable_roots",
                "pending_read_roots", "read_label_roots",
                "pending_read_label_roots"):
        values = state.get(key, [])
        if not isinstance(values, list) or not all(
                isinstance(value, str) and os.path.isabs(value) for value in values):
            raise ValueError("The Python sandbox ACL state is invalid.")
        state[key] = sorted({os.path.normcase(value) for value in values})
    protected = state.get("protected_paths", {})
    if not isinstance(protected, dict) or not all(
            isinstance(path, str) and os.path.isabs(path)
            and isinstance(allow_read, bool)
            for path, allow_read in protected.items()):
        raise ValueError("The Python sandbox ACL state is invalid.")
    state["protected_paths"] = {
        os.path.normcase(path): allow_read for path, allow_read in protected.items()}
    return state


def _save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
        _protect_path(temporary, state["sid"], False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _security_error(action, code):
    return OSError(f"{action} failed (Windows error {code}).")


def _path_is_protected(path, sid, allow_read):
    kernel, advapi, _ = _apis()
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    handle = None
    if _is_reparse_point(path):
        handle = _open_reparse_point(path, _READ_CONTROL)
        code = advapi.GetSecurityInfo(
            handle, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        action = f"GetSecurityInfo({path})"
    else:
        code = advapi.GetNamedSecurityInfoW(
            path, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        action = f"GetNamedSecurityInfoW({path})"
    if code:
        if descriptor:
            kernel.LocalFree(descriptor)
        _close(handle)
        raise _security_error(action, code)
    try:
        control = wintypes.WORD()
        revision = wintypes.WORD()
        if not advapi.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise _win_error("GetSecurityDescriptorControl")
        if not control.value & _DACL_PROTECTED or not dacl.value:
            return False
        header = ctypes.string_at(dacl, 8)
        acl_size = struct.unpack_from("<H", header, 2)[0]
        ace_count = struct.unpack_from("<H", header, 4)[0]
        raw = ctypes.string_at(dacl, acl_size)
        package_aces = []
        package_group_aces = []
        offset = 8
        for _ in range(ace_count):
            if offset + 4 > acl_size:
                raise OSError(f"Malformed DACL for {path}")
            ace_size = struct.unpack_from("<H", raw, offset + 2)[0]
            if ace_size < 4 or offset + ace_size > acl_size:
                raise OSError(f"Malformed ACE for {path}")
            ace_type = raw[offset]
            if ace_type in (_ACCESS_ALLOWED_ACE, _ACCESS_DENIED_ACE):
                if ace_size < 12:
                    raise OSError(f"Malformed access ACE for {path}")
                ace_sid = _sid_string(dacl.value + offset + 8)
                mask = struct.unpack_from("<I", raw, offset + 4)[0]
                if ace_sid == sid:
                    package_aces.append((ace_type, mask))
                elif ace_sid == _ALL_APPLICATION_PACKAGES:
                    package_group_aces.append((ace_type, mask))
            offset += ace_size
        if offset != acl_size:
            raise OSError(f"Malformed DACL size for {path}")
        if package_group_aces:
            return False
        if not allow_read:
            return not package_aces
        return (bool(package_aces)
                and all(ace_type == _ACCESS_ALLOWED_ACE and not mask & _WRITE_ACCESS
                        for ace_type, mask in package_aces))
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        _close(handle)


def _protect_path(path, sid, allow_read):
    kernel, advapi, _ = _apis()
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    handle = None
    if _is_reparse_point(path):
        handle = _open_reparse_point(path, _READ_CONTROL | _WRITE_DAC)
        code = advapi.GetSecurityInfo(
            handle, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        action = f"GetSecurityInfo({path})"
    else:
        code = advapi.GetNamedSecurityInfoW(
            path, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        action = f"GetNamedSecurityInfoW({path})"
    if code:
        if descriptor:
            kernel.LocalFree(descriptor)
        _close(handle)
        raise _security_error(action, code)
    package_sid = ctypes.c_void_p()
    new_acl = ctypes.c_void_p()
    try:
        if not dacl.value:
            raise OSError(f"Cannot protect a path without a DACL: {path}")
        original = ctypes.string_at(dacl, 8)
        acl_size = struct.unpack_from("<H", original, 2)[0]
        ace_count = struct.unpack_from("<H", original, 4)[0]
        revision = original[0]
        if revision not in (2, 3, 4):
            raise OSError(f"Unsupported DACL revision for {path}: {revision}")
        raw = ctypes.string_at(dacl, acl_size)
        kept = []
        offset = 8
        for _ in range(ace_count):
            if offset + 4 > acl_size:
                raise OSError(f"Malformed DACL for {path}")
            ace_size = struct.unpack_from("<H", raw, offset + 2)[0]
            if ace_size < 4 or offset + ace_size > acl_size:
                raise OSError(f"Malformed ACE for {path}")
            ace_type = raw[offset]
            package_ace = False
            if ace_type in (_ACCESS_ALLOWED_ACE, _ACCESS_DENIED_ACE):
                if ace_size < 12:
                    raise OSError(f"Malformed access ACE for {path}")
                ace_sid = _sid_string(dacl.value + offset + 8)
                package_ace = ace_sid in (sid, _ALL_APPLICATION_PACKAGES)
            if not package_ace:
                kept.append(raw[offset:offset + ace_size])
            offset += ace_size
        if offset != acl_size:
            raise OSError(f"Malformed DACL size for {path}")

        new_size = 8 + sum(map(len, kept)) + (256 if allow_read else 0)
        if new_size > 0xFFFF:
            raise OSError(f"DACL is too large to protect: {path}")
        new_acl = kernel.LocalAlloc(_LMEM_FIXED, new_size)
        if not new_acl:
            raise _win_error("LocalAlloc")
        if not advapi.InitializeAcl(new_acl, new_size, revision):
            raise _win_error("InitializeAcl")
        for ace in kept:
            buffer = ctypes.create_string_buffer(ace)
            if not advapi.AddAce(
                    new_acl, revision, _MAXDWORD, buffer, len(ace)):
                raise _win_error("AddAce")
        if allow_read:
            if not advapi.ConvertStringSidToSidW(sid, ctypes.byref(package_sid)):
                raise _win_error("ConvertStringSidToSidW")
            flags = _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE
            if not advapi.AddAccessAllowedAceEx(
                    new_acl, revision, flags,
                    _GENERIC_READ | _GENERIC_EXECUTE, package_sid):
                raise _win_error("AddAccessAllowedAceEx")

        if handle:
            code = advapi.SetSecurityInfo(
                handle, _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, new_acl, None)
            action = f"SetSecurityInfo({path})"
        else:
            name = ctypes.create_unicode_buffer(path)
            code = advapi.SetNamedSecurityInfoW(
                name, _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, new_acl, None)
            action = f"SetNamedSecurityInfoW({path})"
        if code:
            raise _security_error(action, code)
    finally:
        if package_sid:
            kernel.LocalFree(package_sid)
        if new_acl:
            kernel.LocalFree(new_acl)
        if descriptor:
            kernel.LocalFree(descriptor)
        _close(handle)


def _protected_paths(roots, state_path, *, stop_event=None, deadline=None):
    import config

    _check_host_work(stop_event, deadline)
    protected = []
    protected_directories = {".git", ".venv"}
    protected_names = protected_directories | {
        ".env", "credentials.json", "agent_user_session.session",
        "agent_docs_session.session", "agentbot_memory.md",
    }
    normalized_roots = tuple(
        os.path.normcase(os.path.realpath(root)) for root in roots)

    def fail_scan(error):
        raise error

    def inside_approved_roots(path):
        normalized = os.path.normcase(path)
        for root in normalized_roots:
            try:
                if os.path.commonpath([root, normalized]) == root:
                    return True
            except ValueError:
                continue
        return False

    for root in roots:
        _check_host_work(stop_event, deadline)
        if (os.path.basename(root).casefold() in protected_directories
                and os.path.isdir(root)):
            protected.append((root, True))
        for current, directories, files in os.walk(
                root, onerror=fail_scan, followlinks=False):
            _check_host_work(stop_event, deadline)
            for name in tuple(directories):
                _check_host_work(stop_event, deadline)
                path = os.path.join(current, name)
                # Windows junctions are not symlinks, so os.walk still enters them.
                if _is_reparse_point(path):
                    directories.remove(name)
                    if name.casefold() in protected_directories:
                        target = os.path.realpath(path)
                        if not os.path.isdir(target):
                            raise NotADirectoryError(
                                f"Protected sandbox junction has no directory target: {path}")
                        if not inside_approved_roots(target):
                            raise ValueError(
                                f"Protected sandbox junction points outside approved roots: {path}")
                        protected.append((path, True))
                        protected.append((target, True))
                    continue
                if name.casefold() in protected_directories:
                    protected.append((path, True))
                    directories.remove(name)
            for name in files:
                _check_host_work(stop_event, deadline)
                if name.casefold() in protected_names:
                    path = os.path.join(current, name)
                    protected.append((
                        path,
                        name.casefold() in protected_directories
                        and os.path.isdir(path),
                    ))
    for path in (config.CONFIG_PATH, config.memory_dir(), state_path):
        _check_host_work(stop_event, deadline)
        if os.path.lexists(path):
            protected.append((path, False))
    configured_session = str(
        config.load_config().get("TG_DOC_SESSION") or "agent_docs_session").strip()
    module_dir = os.path.dirname(os.path.abspath(__file__))
    for session_name in {"agent_docs_session", configured_session}:
        _check_host_work(stop_event, deadline)
        clean = os.path.basename(session_name) or "agent_docs_session"
        if not clean.casefold().endswith(".session"):
            clean += ".session"
        session_path = os.path.join(module_dir, clean)
        if os.path.lexists(session_path):
            protected.append((session_path, False))
    return sorted(set(protected))


def _configure_access(sid, writable, readable, state_path, project_dir, *,
                      stop_event=None, deadline=None):
    scan_roots = sorted(set(readable) - set(writable))
    scans = _start_read_root_scans(
        scan_roots, sid, stop_event=stop_event, deadline=deadline)
    try:
        _configure_access_with_scans(
            sid, writable, readable, state_path, project_dir, scans,
            stop_event=stop_event, deadline=deadline)
    finally:
        _stop_read_root_scans(scans)


def _configure_access_with_scans(sid, writable, readable, state_path, project_dir,
                                 scans, *, stop_event=None, deadline=None):
    _check_host_work(stop_event, deadline)
    state = _load_state(state_path, sid)
    previous_protected = state["protected_paths"]
    legacy_write_grant = state["acl_version"] != _ACL_VERSION
    active_w = set(state["writable_roots"])
    active_r = set(state["read_roots"])
    pending_w = set(state["pending_writable_roots"])
    pending_r = set(state["pending_read_roots"])
    active_labels = set(state["read_label_roots"])
    pending_labels = set(state["pending_read_label_roots"])
    known_labels = active_labels | pending_labels
    desired_w = set(writable)
    desired_r = set(readable) - desired_w
    for writable_path in desired_w:
        for readable_path in desired_r:
            if writable_path != readable_path and _is_nested_path(
                    writable_path, readable_path):
                raise ValueError(
                    "A writable Python sandbox root cannot be nested below "
                    "a read-only root.")
    desired_access = {path: _MODIFY_ACCESS for path in desired_w}
    desired_access.update({
        path: _GENERIC_READ | _GENERIC_EXECUTE for path in desired_r})
    known = active_w | active_r | pending_w | pending_r | known_labels
    matching_desired = set()
    for path in sorted(desired_access):
        _check_host_work(stop_event, deadline)
        if _root_access_matches(
                path, sid, desired_access[path],
                stop_event=stop_event, deadline=deadline):
            matching_desired.add(path)
    matching_pending = matching_desired & (pending_w | pending_r)
    active_w |= matching_pending & desired_w
    active_r |= matching_pending & desired_r
    changed = {
        path for path in known
        if (path in pending_w or path in pending_r)
        and path not in matching_pending
        or (legacy_write_grant and path in (active_w | pending_w))
        or ((path in active_w) != (path in desired_w))
        or ((path in active_r) != (path in desired_r))
        or (path in desired_access and path not in matching_desired)
    }
    grant_w = desired_w - (active_w - changed)
    grant_r = desired_r - (active_r - changed)
    label_states = {}
    for path in sorted(known | set(desired_access)):
        _check_host_work(stop_event, deadline)
        label_states[path] = _root_label_state(
            path, stop_event=stop_event, deadline=deadline)
        if label_states[path] == "unsafe":
            raise OSError(
                f"Unsafe mandatory label for Python sandbox root: {path}")
    for path in desired_w:
        if label_states.get(path) in ("direct", "inherited") \
                and path not in known_labels:
            raise OSError(
                f"A mandatory label blocks writable Python sandbox root: {path}")
    new_labels = {
        path for path in grant_r
        if path in known_labels or label_states.get(path) == "none"}
    retained_labels = (known_labels & desired_r) | new_labels
    clear_label_paths = changed & known_labels
    _apply_access_changes(
        {path: None for path in changed}, sid,
        stop_event=stop_event, deadline=deadline,
        clear_label_paths=clear_label_paths)
    state = _empty_state(sid)
    state["writable_roots"] = sorted(active_w - changed)
    state["read_roots"] = sorted(active_r - changed)
    state["pending_writable_roots"] = sorted(grant_w)
    state["pending_read_roots"] = sorted(grant_r)
    state["read_label_roots"] = sorted(known_labels - changed)
    state["pending_read_label_roots"] = sorted(new_labels)
    state["protected_paths"] = dict(previous_protected)
    # Pending paths let the next run revoke grants left by an interrupted setup.
    _check_host_work(stop_event, deadline)
    _save_state(state_path, state)
    _apply_access_changes(
        {**{path: _MODIFY_ACCESS for path in grant_w},
         **{path: _GENERIC_READ | _GENERIC_EXECUTE for path in grant_r}},
        sid, stop_event=stop_event, deadline=deadline)
    findings = _finish_read_root_scans(
        scans, stop_event=stop_event, deadline=deadline)
    unsafe_group = next(
        (item["path"] for item in findings if item["kind"] == "group"), None)
    if unsafe_group is not None:
        raise OSError(
            "Unsafe ALL_APPLICATION_PACKAGES access below Python sandbox "
            f"read root: {unsafe_group}")
    direct_writes = {
        item["path"] for item in findings if item["kind"] == "write"}
    if direct_writes:
        _apply_access_changes(
            {path: _GENERIC_READ | _GENERIC_EXECUTE for path in direct_writes},
            sid, stop_event=stop_event, deadline=deadline)
        for path in direct_writes:
            if not _root_access_matches(
                    path, sid, _GENERIC_READ | _GENERIC_EXECUTE,
                    stop_event=stop_event, deadline=deadline):
                raise OSError(
                    f"Cannot remediate Python sandbox read-root access: {path}")
    protected = {}
    scan_roots = _collapse_roots([*desired_w, project_dir])
    discovered = _protected_paths(
        scan_roots, state_path, stop_event=stop_event, deadline=deadline)
    recovery_protected = dict(previous_protected)
    for path, allow_read in discovered:
        key = os.path.normcase(path)
        # Readable recovery entries revoke grants even if protection is interrupted.
        if key not in recovery_protected or (
                allow_read and not recovery_protected[key]):
            recovery_protected[key] = True
    if recovery_protected != previous_protected:
        state["protected_paths"] = recovery_protected
        _check_host_work(stop_event, deadline)
        _save_state(state_path, state)
    for path, allow_read in discovered:
        _check_host_work(stop_event, deadline)
        key = os.path.normcase(path)
        if (previous_protected.get(key) is allow_read
                and _path_is_protected(path, sid, allow_read)):
            protected[key] = allow_read
            continue
        _protect_path(path, sid, allow_read)
        protected[key] = allow_read
    for path, allow_read in previous_protected.items():
        _check_host_work(stop_event, deadline)
        if not allow_read or path in protected or not os.path.lexists(path):
            continue
        if _is_reparse_point(path):
            _protect_path(path, sid, False)
        elif not _revoke_access(
                path, sid, stop_event=stop_event, deadline=deadline,
                clear_label=False):
            raise OSError(f"Cannot revoke retired Python sandbox access: {path}")
    state = _empty_state(sid)
    state["writable_roots"] = sorted(desired_w)
    state["read_roots"] = sorted(desired_r)
    state["read_label_roots"] = sorted(retained_labels)
    state["protected_paths"] = protected
    _check_host_work(stop_event, deadline)
    _save_state(state_path, state)


def _remove_run_tree(path, sid, *, deadline=None):
    if not os.path.lexists(path):
        return
    _check_host_work(deadline=deadline)
    _run_icacls(path, ["/T", "/C", "/remove", f"*{sid}"], deadline=deadline)
    _check_host_work(deadline=deadline)
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    else:
        shutil.rmtree(path)
    if os.path.lexists(path):
        raise OSError(f"Cannot clean up the private Python sandbox directory: {path}")


def _prepare_run_dir(state_path, profile_name, sid, *, stop_event=None, deadline=None):
    _check_host_work(stop_event, deadline)
    suffix = profile_name.rsplit(".", 1)[-1]
    root = os.path.join(os.path.dirname(state_path), "PythonSandboxRuns", suffix)
    os.makedirs(root, exist_ok=True)
    with os.scandir(root) as entries:
        stale = [entry.path for entry in entries]
    for path in stale:
        _check_host_work(stop_event, deadline)
        _remove_run_tree(path, sid, deadline=deadline)
    _check_host_work(stop_event, deadline)
    if not _run_icacls(root, ["/grant", f"*{sid}:(OI)(CI)(RX)"],
                       stop_event=stop_event, deadline=deadline):
        raise OSError("Cannot grant access to the private Python sandbox directory.")
    run_dir = os.path.join(root, f"{os.getpid()}_{uuid.uuid4().hex}")
    try:
        os.makedirs(run_dir)
        _check_host_work(stop_event, deadline)
        if not _run_icacls(run_dir, ["/grant", f"*{sid}:(OI)(CI)(M)"],
                           stop_event=stop_event, deadline=deadline):
            raise OSError(
                "Cannot grant access to the private Python sandbox run directory.")
    except Exception:
        try:
            _remove_run_tree(run_dir, sid)
        except Exception as cleanup_error:
            raise OSError(
                f"Cannot clean up the private Python sandbox run directory: "
                f"{cleanup_error}") from cleanup_error
        raise
    return run_dir


def _create_job(kernel):
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise _win_error("CreateJobObjectW")
    limits = _ExtendedLimitInformation()
    flags = (_JOB_KILL_ON_CLOSE | _JOB_ACTIVE_PROCESS
             | _JOB_PROCESS_MEMORY | _JOB_JOB_MEMORY)
    limits.BasicLimitInformation.LimitFlags = flags
    limits.BasicLimitInformation.ActiveProcessLimit = _PROCESS_LIMIT
    limits.ProcessMemoryLimit = _PROCESS_MEMORY
    limits.JobMemoryLimit = _JOB_MEMORY
    if not kernel.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        _close(job)
        raise _win_error("SetInformationJobObject")
    return job


def _environment(run_dir, profile_dir):
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    values = {
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "ComSpec": os.environ.get("ComSpec", os.path.join(system_root, "System32", "cmd.exe")),
        "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
        "LOCALAPPDATA": profile_dir,
        "APPDATA": profile_dir,
        "USERPROFILE": run_dir,
        "HOME": run_dir,
        "TEMP": run_dir,
        "TMP": run_dir,
    }
    if len(run_dir) >= 2 and run_dir[1] == ":":
        values["HOMEDRIVE"] = run_dir[:2]
        values["HOMEPATH"] = run_dir[2:]
    for name in ("PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432",
                 "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL", "PROCESSOR_REVISION"):
        if os.environ.get(name):
            values[name] = os.environ[name]
    block = "\0".join(
        f"{name}={value}" for name, value in sorted(
            values.items(), key=lambda item: item[0].upper())) + "\0\0"
    return ctypes.create_unicode_buffer(block)


def _start_process(interpreter, code_path, runner_path, cwd, readable,
                   run_dir, profile_dir, job, profile_name):
    kernel, advapi, userenv = _apis()
    stdin_read = wintypes.HANDLE()
    stdout_read = wintypes.HANDLE()
    stdout_write = wintypes.HANDLE()
    stderr_read = wintypes.HANDLE()
    stderr_write = wintypes.HANDLE()
    receipt_read = wintypes.HANDLE()
    receipt_write = wintypes.HANDLE()
    receipt_ack_read = wintypes.HANDLE()
    receipt_ack_write = wintypes.HANDLE()
    process_info = _ProcessInformation()
    startup = _StartupInfoEx()
    attribute_buffer = None
    profile_sid = ctypes.c_void_p()
    started = False
    try:
        code = userenv.DeriveAppContainerSidFromAppContainerName(
            profile_name, ctypes.byref(profile_sid))
        if code != 0:
            raise _hresult_error("DeriveAppContainerSidFromAppContainerName", code)
        stdin_read.value = kernel.CreateFileW(
            "NUL", 0x80000000, 0x00000003, None, 3, 0, None)
        if stdin_read.value == ctypes.c_void_p(-1).value:
            raise _win_error("CreateFileW(NUL)")
        if not kernel.CreatePipe(
                ctypes.byref(stdout_read), ctypes.byref(stdout_write), None, 0):
            raise _win_error("CreatePipeW(stdout)")
        if not kernel.CreatePipe(
                ctypes.byref(stderr_read), ctypes.byref(stderr_write), None, 0):
            raise _win_error("CreatePipeW(stderr)")
        if not kernel.CreatePipe(
                ctypes.byref(receipt_read), ctypes.byref(receipt_write), None, 0):
            raise _win_error("CreatePipeW(receipts)")
        if not kernel.CreatePipe(
                ctypes.byref(receipt_ack_read), ctypes.byref(receipt_ack_write), None, 0):
            raise _win_error("CreatePipeW(receipt acknowledgements)")
        for handle in (stdin_read, stdout_write, stderr_write,
                       receipt_write, receipt_ack_read):
            if not kernel.SetHandleInformation(handle, _HANDLE_FLAG_INHERIT, 1):
                raise _win_error("SetHandleInformation")

        size = ctypes.c_size_t()
        kernel.InitializeProcThreadAttributeList(None, 3, 0, ctypes.byref(size))
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
            raise _win_error("InitializeProcThreadAttributeList(size)")
        attribute_buffer = ctypes.create_string_buffer(size.value)
        if not kernel.InitializeProcThreadAttributeList(
                attribute_buffer, 3, 0, ctypes.byref(size)):
            raise _win_error("InitializeProcThreadAttributeList")
        capabilities = _SecurityCapabilities()
        capabilities.AppContainerProfile = profile_sid
        if not kernel.UpdateProcThreadAttribute(
                attribute_buffer, 0, _SECURITY_CAPABILITIES,
                ctypes.byref(capabilities), ctypes.sizeof(capabilities), None, None):
            raise _win_error("UpdateProcThreadAttribute(security)")
        handles = (wintypes.HANDLE * 5)(
            stdin_read, stdout_write, stderr_write, receipt_write, receipt_ack_read)
        if not kernel.UpdateProcThreadAttribute(
                attribute_buffer, 0, _HANDLE_LIST, handles, ctypes.sizeof(handles),
                None, None):
            raise _win_error("UpdateProcThreadAttribute(handles)")
        jobs = (wintypes.HANDLE * 1)(job)
        if not kernel.UpdateProcThreadAttribute(
                attribute_buffer, 0, _JOB_LIST, jobs, ctypes.sizeof(jobs),
                None, None):
            raise _win_error("UpdateProcThreadAttribute(job)")

        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = stdin_read
        startup.StartupInfo.hStdOutput = stdout_write
        startup.StartupInfo.hStdError = stderr_write
        startup.AttributeList = ctypes.cast(attribute_buffer, ctypes.c_void_p)
        arguments = [interpreter, "-I", "-S", "-X", "utf8", runner_path,
                     code_path, cwd, run_dir, str(receipt_write.value),
                     str(receipt_ack_read.value), *readable]
        command_line = subprocess.list2cmdline(arguments)
        flags = (_EXTENDED_STARTUPINFO_PRESENT | _CREATE_NO_WINDOW
                 | _CREATE_SUSPENDED | _CREATE_UNICODE_ENVIRONMENT)
        environment = _environment(run_dir, profile_dir)
        if not kernel.CreateProcessW(
                interpreter, ctypes.create_unicode_buffer(command_line), None, None,
                True, flags, environment, cwd,
                ctypes.cast(ctypes.byref(startup), ctypes.POINTER(_StartupInfo)),
                ctypes.byref(process_info)):
            raise _win_error("CreateProcessW")
        started = True
        return (process_info.hProcess, process_info.hThread, stdout_read,
                stderr_read, receipt_read, receipt_ack_write)
    finally:
        if profile_sid:
            advapi.FreeSid(profile_sid)
        _close(stdin_read)
        _close(stdout_write)
        _close(stderr_write)
        _close(receipt_write)
        _close(receipt_ack_read)
        if attribute_buffer is not None:
            kernel.DeleteProcThreadAttributeList(attribute_buffer)
        if not started:
            _close(stdout_read)
            _close(stderr_read)
            _close(receipt_read)
            _close(receipt_ack_write)
            _close(process_info.hProcess)
            _close(process_info.hThread)


def _token_integrity_below_medium(token):
    needed = wintypes.DWORD()
    advapi = _apis()[1]
    if advapi.GetTokenInformation(
            token, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(needed)):
        raise _win_error("GetTokenInformation(TokenIntegrityLevel)")
    if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
        raise _win_error("GetTokenInformation(TokenIntegrityLevel)")
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi.GetTokenInformation(
            token, _TOKEN_INTEGRITY_LEVEL, buffer, len(buffer),
            ctypes.byref(needed)):
        raise _win_error("GetTokenInformation(TokenIntegrityLevel)")
    label = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents
    if not label.Label.Sid:
        return False
    sid = _sid_string(label.Label.Sid)
    parts = sid.split("-")
    if len(parts) < 3 or parts[-2] != "16":
        return False
    try:
        integrity_rid = int(parts[-1])
    except ValueError:
        return False
    return integrity_rid < _MEDIUM_INTEGRITY_RID


def _verify_process(process, job):
    kernel, advapi, _ = _apis()
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
        raise _win_error("OpenProcessToken(sandbox child)")
    try:
        value = wintypes.DWORD()
        needed = wintypes.DWORD()
        if not advapi.GetTokenInformation(
                token, _TOKEN_IS_APP_CONTAINER, ctypes.byref(value),
                ctypes.sizeof(value), ctypes.byref(needed)):
            raise _win_error("GetTokenInformation(TokenIsAppContainer)")
        app_container = value.value == 1
        low_integrity = _token_integrity_below_medium(token)
    finally:
        _close(token)
    in_job = wintypes.BOOL()
    if not kernel.IsProcessInJob(process, job, ctypes.byref(in_job)):
        raise _win_error("IsProcessInJob")
    return app_container, low_integrity, in_job.value == 1


class _PipeReader:
    def __init__(self, handle, max_output):
        self.handle = handle
        self.max_bytes = max_output * 4 + 4
        self.buffer = bytearray()
        self.truncated = False
        self.error = None
        self.thread = threading.Thread(target=self._read, daemon=True)

    def _read(self):
        kernel = _apis()[0]
        chunk = ctypes.create_string_buffer(65536)
        try:
            while True:
                read = wintypes.DWORD()
                if not kernel.ReadFile(
                        self.handle, chunk, len(chunk), ctypes.byref(read), None):
                    code = ctypes.get_last_error()
                    if code != _ERROR_BROKEN_PIPE:
                        self.error = f"Sandbox output read failed ({code})."
                    break
                if read.value == 0:
                    break
                remaining = self.max_bytes - len(self.buffer)
                if remaining > 0:
                    self.buffer.extend(chunk.raw[:min(read.value, remaining)])
                if read.value > remaining:
                    self.truncated = True
        finally:
            _close(self.handle)

    def start(self):
        self.thread.start()

    def finish(self, max_output):
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            self.error = "Sandbox output pipes did not close after termination."
        text = bytes(self.buffer).decode("utf-8", errors="replace")
        if len(text) > max_output:
            text = text[:max_output]
            self.truncated = True
        return text


class _MutationBroker:
    def __init__(self, request_handle, ack_handle, project_dir, writable_roots):
        from verification_adapter import snapshot_python_file

        self.request_handle = request_handle
        self.ack_handle = ack_handle
        self.project_dir = os.path.normcase(os.path.abspath(project_dir))
        self.roots = tuple(os.path.normcase(os.path.abspath(root))
                           for root in writable_roots)
        self.snapshot_file = snapshot_python_file
        self.snapshots = {}
        self.requests = 0
        self.error = None
        self.thread = threading.Thread(target=self._read, daemon=True)

    def start(self):
        self.thread.start()

    def _canonical_path(self, value):
        if not isinstance(value, str) or not value or "\x00" in value:
            return None
        try:
            path = os.path.realpath(os.path.abspath(os.path.join(self.project_dir, value)))
            normalized = os.path.normcase(path)
            for root in self.roots:
                try:
                    if os.path.commonpath([root, normalized]) == root:
                        return normalized
                except ValueError:
                    continue
        except (OSError, ValueError):
            pass
        return None

    def _ack(self, kernel, status):
        written = wintypes.DWORD()
        payload = {0: b"0", 1: b"1", 2: b"2"}[status]
        if not kernel.WriteFile(self.ack_handle, payload,
                                1, ctypes.byref(written), None):
            self.error = f"Sandbox receipt acknowledgement failed ({ctypes.get_last_error()})."

    def _handle(self, kernel, line):
        try:
            request = json.loads(line.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            self.error = "Sandbox sent a malformed receipt request."
            self._ack(kernel, False)
            return
        if not isinstance(request, dict) or set(request) != {"path"}:
            self.error = "Sandbox sent an invalid receipt request."
            self._ack(kernel, False)
            return
        if self.requests >= _RECEIPT_MAX_REQUESTS:
            self.error = "Sandbox mutation receipt request limit exceeded."
            self._ack(kernel, False)
            return
        self.requests += 1
        key = self._canonical_path(request["path"])
        if key is None:
            self._ack(kernel, 2)
            return
        if key in self.snapshots:
            self._ack(kernel, True)
            return
        if len(self.snapshots) >= _RECEIPT_MAX_TARGETS:
            self.error = "Sandbox mutation receipt target limit exceeded."
            self._ack(kernel, False)
            return
        try:
            state = self.snapshot_file(key)
        except Exception as exc:  # noqa: BLE001 - snapshot failures deny mutation evidence
            self.error = f"Sandbox mutation snapshot failed: {type(exc).__name__}."
            self._ack(kernel, False)
            return
        self.snapshots[key] = state
        self._ack(kernel, True)

    def _read(self):
        kernel = _apis()[0]
        chunk = ctypes.create_string_buffer(65536)
        buffer = bytearray()
        try:
            while True:
                read = wintypes.DWORD()
                if not kernel.ReadFile(
                        self.request_handle, chunk, len(chunk), ctypes.byref(read), None):
                    code = ctypes.get_last_error()
                    if code != _ERROR_BROKEN_PIPE:
                        self.error = f"Sandbox receipt read failed ({code})."
                    break
                if read.value == 0:
                    break
                buffer.extend(chunk.raw[:read.value])
                while True:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    line = bytes(buffer[:newline])
                    del buffer[:newline + 1]
                    if len(line) > _RECEIPT_MAX_LINE:
                        self.error = "Sandbox receipt request was too large."
                        self._ack(kernel, False)
                        return
                    self._handle(kernel, line)
                    if self.error:
                        return
                if len(buffer) > _RECEIPT_MAX_LINE:
                    self.error = "Sandbox receipt request was too large."
                    return
        finally:
            _close(self.request_handle)
            _close(self.ack_handle)

    def finish(self):
        self.thread.join(timeout=5)
        if self.thread.is_alive() and self.error is None:
            self.error = "Sandbox receipt pipe did not close after termination."
        return dict(self.snapshots)


def _wait_for_process(process, stop_event, deadline):
    kernel = _apis()[0]
    while True:
        status = kernel.WaitForSingleObject(process, 100)
        if status == _WAIT_OBJECT_0:
            return "finished"
        if status == _WAIT_TIMEOUT:
            if stop_event is not None and stop_event.is_set():
                return "cancelled"
            if time.monotonic() >= deadline:
                return "timed_out"
            continue
        if status == _WAIT_ABANDONED:
            return "finished"
        raise _win_error("WaitForSingleObject(sandbox child)")


def _acquire_mutex(handle, stop_event, deadline=None):
    kernel = _apis()[0]
    wait_deadline = time.monotonic() + _MUTEX_WAIT
    if deadline is not None:
        wait_deadline = min(wait_deadline, deadline)
    while True:
        if stop_event is not None and stop_event.is_set():
            return "cancelled"
        if time.monotonic() >= wait_deadline:
            return "timed_out"
        status = kernel.WaitForSingleObject(handle, 100)
        if status in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
            return "acquired"
        if status != _WAIT_TIMEOUT:
            raise _win_error("WaitForSingleObject(sandbox mutex)")


def _acquire_run_lock(stop_event, deadline=None):
    while True:
        if stop_event is not None and stop_event.is_set():
            return "cancelled"
        if deadline is not None and time.monotonic() >= deadline:
            return "timed_out"
        timeout = 0.1
        if deadline is not None:
            timeout = min(timeout, max(0.0, deadline - time.monotonic()))
        if _RUN_LOCK.acquire(timeout=timeout):
            if stop_event is not None and stop_event.is_set():
                _RUN_LOCK.release()
                return "cancelled"
            if deadline is not None and time.monotonic() >= deadline:
                _RUN_LOCK.release()
                return "timed_out"
            return "acquired"


def run_python(code, *, cwd, writable_roots, stop_event=None, timeout=60,
               max_output=12000):
    if not (code or "").strip():
        return SandboxResult(error="ERROR: execute_python needs code.")
    if stop_event is not None and stop_event.is_set():
        return SandboxResult(cancelled=True)
    if os.name != "nt":
        return SandboxResult(error="ERROR: the Python sandbox requires Windows.")
    if timeout <= 0 or max_output <= 0:
        return SandboxResult(error="ERROR: invalid Python sandbox limits.")

    process = None
    thread = None
    job = None
    mutex = None
    sid = None
    run_dir = None
    stdout_reader = None
    stderr_reader = None
    mutation_broker = None
    result = SandboxResult()
    lock_acquired = False
    mutex_acquired = False
    setup_deadline = time.monotonic() + _SETUP_TIMEOUT
    try:
        interpreter = _resolve_interpreter()
        project_dir = _normalize_roots([cwd])[0]
        roots = _default_roots() if writable_roots is None else writable_roots
        writable = _normalize_roots(roots)
        readable = _read_roots(project_dir, interpreter)
        profile_name, _, state_path = _profile_identity()
        runtime_dir = os.path.normcase(os.path.dirname(interpreter))
        state_dir = os.path.normcase(os.path.dirname(state_path))
        runtime_path = Path(runtime_dir)
        runtime_venv = next((
            os.path.normcase(str(candidate))
            for candidate in (runtime_path, *runtime_path.parents)
            if candidate.name.casefold() == ".venv"
        ), None)

        def overlaps(root, target):
            try:
                common = os.path.commonpath([root, target])
            except ValueError:
                return False
            return common in (root, target)

        for root in writable:
            if overlaps(root, state_dir):
                raise ValueError(
                    "A writable sandbox root cannot contain the Python runtime "
                    "or Kestrel ACL state.")
            if runtime_venv is None:
                if overlaps(root, runtime_dir):
                    raise ValueError(
                        "A writable sandbox root cannot contain the Python runtime "
                        "or Kestrel ACL state.")
                continue
            try:
                protects_venv = os.path.commonpath([root, runtime_venv]) == root
            except ValueError:
                protects_venv = False
            if (overlaps(root, runtime_dir) or overlaps(root, runtime_venv)) \
                    and not protects_venv:
                raise ValueError(
                    "A writable sandbox root cannot contain the Python runtime "
                    "or Kestrel ACL state.")
        lock_status = _acquire_run_lock(stop_event, setup_deadline)
        if lock_status == "cancelled":
            result.cancelled = True
            return result
        if lock_status != "acquired":
            raise OSError(
                "Python sandbox setup time limit exceeded while waiting for the run lock.")
        lock_acquired = True
        kernel = _apis()[0]
        mutex = kernel.CreateMutexW(
            None, False, f"Local\\KestrelPythonSandbox-{profile_name.rsplit('.', 1)[-1]}")
        if not mutex:
            raise _win_error("CreateMutexW")
        acquired = _acquire_mutex(mutex, stop_event, setup_deadline)
        if acquired == "cancelled":
            result.cancelled = True
            return result
        if acquired != "acquired":
            raise OSError(
                "Python sandbox setup time limit exceeded while waiting for the sandbox mutex.")
        mutex_acquired = True
        if stop_event is not None and stop_event.is_set():
            result.cancelled = True
            return result
        _check_host_work(stop_event, setup_deadline)
        sid = _ensure_profile(profile_name, state_path)
        _configure_access(
            sid, writable, readable, state_path, project_dir,
            stop_event=stop_event, deadline=setup_deadline)
        if stop_event is not None and stop_event.is_set():
            result.cancelled = True
            return result
        run_dir = _prepare_run_dir(
            state_path, profile_name, sid, stop_event=stop_event,
            deadline=setup_deadline)
        runner_path = os.path.join(run_dir, "runner.py")
        code_path = os.path.join(run_dir, "code.py")
        with open(runner_path, "x", encoding="utf-8") as handle:
            handle.write(_RUNNER)
        with open(code_path, "xb") as handle:
            handle.write(code.encode("utf-8", errors="surrogatepass"))
        _check_host_work(stop_event, setup_deadline)

        job = _create_job(kernel)
        (process, thread, stdout_handle, stderr_handle,
         receipt_read, receipt_ack_write) = _start_process(
            interpreter, code_path, runner_path, project_dir, readable,
            run_dir, run_dir, job, profile_name)
        mutation_broker = _MutationBroker(
            receipt_read, receipt_ack_write, project_dir, writable)
        mutation_broker.start()
        stdout_reader = _PipeReader(stdout_handle, max_output)
        stderr_reader = _PipeReader(stderr_handle, max_output)
        stdout_reader.start()
        stderr_reader.start()
        (result.app_container, low_integrity,
         result.job_controlled) = _verify_process(process, job)
        if not result.app_container or not low_integrity or not result.job_controlled:
            raise OSError(
                "The Python child failed its AppContainer/integrity/Job verification.")
        _check_host_work(stop_event, setup_deadline)
        suspended = kernel.ResumeThread(thread)
        if suspended == _MAXDWORD:
            raise _win_error("ResumeThread")
        _close(thread)
        thread = None
        outcome = _wait_for_process(
            process, stop_event, time.monotonic() + timeout)
        if outcome == "finished":
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                raise _win_error("GetExitCodeProcess")
            result.returncode = exit_code.value
        else:
            result.timed_out = outcome == "timed_out"
            result.cancelled = outcome == "cancelled"
    except InterruptedError:
        result.cancelled = True
    except Exception as exc:  # noqa: BLE001 - the sandbox boundary fails closed
        result.error = f"ERROR: cannot run sandboxed Python: {exc}"
    finally:
        if job is not None:
            kernel = _apis()[0]
            kernel.TerminateJobObject(job, 1)
            if process is not None:
                kernel.WaitForSingleObject(process, 5000)
            _close(job)
            job = None
        if process is not None:
            _close(process)
            process = None
        if thread is not None:
            _close(thread)
            thread = None
        if stdout_reader is not None:
            result.stdout = stdout_reader.finish(max_output).strip()
            result.stdout_truncated = stdout_reader.truncated
            if stdout_reader.error and result.error is None:
                result.error = stdout_reader.error
        if stderr_reader is not None:
            result.stderr = stderr_reader.finish(max_output).strip()
            result.stderr_truncated = stderr_reader.truncated
            if stderr_reader.error and result.error is None:
                result.error = stderr_reader.error
        if mutation_broker is not None:
            result.file_snapshots = mutation_broker.finish()
            if mutation_broker.error and result.error is None:
                result.error = f"ERROR: sandbox receipt tracking failed: {mutation_broker.error}"
        if run_dir is not None and sid is not None:
            try:
                _remove_run_tree(run_dir, sid)
            except Exception as exc:  # noqa: BLE001 - report cleanup without masking results
                if result.error is None:
                    result.error = f"ERROR: sandbox cleanup failed: {exc}"
        if mutex_acquired:
            _apis()[0].ReleaseMutex(mutex)
        if mutex is not None:
            _close(mutex)
        if lock_acquired:
            _RUN_LOCK.release()
    return result


if __name__ == "__main__":
    _scan_worker_main()
