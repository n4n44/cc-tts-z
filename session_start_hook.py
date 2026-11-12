#!/usr/bin/env python3
"""Claude Code SessionStart hook (Windows / Linux 共通版): 読み上げデーモンをバックグラウンド起動する。
stdin: {"session_id": ..., "transcript_path": ..., "source": ..., ...}
"""
import json, os, signal, socket, subprocess, sys

DIR = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.expanduser("~/.cache/zunko-tts")
WIN = sys.platform == "win32"
SSH_HOST = os.environ.get("ZUNKO_SSH_HOST", "jamm_gentoo")  # ~/.ssh/config のエイリアス
PORT = 50021
# Windows の Popen フラグ
NO_WINDOW, NEW_GROUP, BREAKAWAY = 0x08000000, 0x00000200, 0x01000000
# 親をたどるときに読み飛ばす中継プロセス
SKIP = {"bash", "sh", "dash", "zsh", "cmd", "powershell", "pwsh", "py", "conhost", "env"}


def proc_table():
    """{pid: (ppid, name)}"""
    if not WIN:
        t = {}
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    stat = open(f"/proc/{d}/stat").read()
                    name = stat[stat.index("(") + 1:stat.rindex(")")]
                    ppid = int(stat[stat.rindex(")") + 2:].split()[1])
                    t[int(d)] = (ppid, name)
                except (OSError, ValueError):
                    pass
        return t
    import ctypes
    from ctypes import wintypes as w

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD),
                    ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    snap = k.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    pe = PE(); pe.dwSize = ctypes.sizeof(PE)
    t = {}
    ok = k.Process32FirstW(snap, ctypes.byref(pe))
    while ok:
        t[pe.th32ProcessID] = (pe.th32ParentProcessID, pe.szExeFile)
        ok = k.Process32NextW(snap, ctypes.byref(pe))
    k.CloseHandle(snap)
    return t


def find_claude_pid():
    t = proc_table()
    p = os.getppid()
    first_other = None
    while p and p in t and p > 1:
        ppid, name = t[p]
        base = name.lower().removesuffix(".exe")
        if base == "claude":
            return p
        if first_other is None and base not in SKIP and not base.startswith("python"):
            first_other = p  # npm 版だと node.exe になる
        p = ppid
    return first_other or os.getppid()


def spawn_bg(cmd, **kw):
    """Claude Code が終了しても残るようにバックグラウンド起動する"""
    if WIN:
        base = NO_WINDOW | NEW_GROUP
        try:
            return subprocess.Popen(cmd, creationflags=base | BREAKAWAY, **kw)
        except OSError:
            return subprocess.Popen(cmd, creationflags=base, **kw)
    return subprocess.Popen(cmd, start_new_session=True, **kw)


def ensure_tunnel():
    """エンジンに届かなければ SSH トンネルを張る (確立待ちはデーモン側に任せる)"""
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=0.5).close()
        return
    except OSError:
        pass
    cmd = ["ssh", "-N", "-L", f"{PORT}:127.0.0.1:{PORT}",
           "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
           "-o", "BatchMode=yes", SSH_HOST]
    log = open(os.path.join(CACHE, "tunnel.log"), "ab")
    proc = spawn_bg(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    open(os.path.join(CACHE, "tunnel.pid"), "w").write(str(proc.pid))


def kill_old_daemon(pidfile):
    """/clear 前の古いデーモンを止める。PID 再利用で別プロセスを殺さないよう python か確認する"""
    try:
        pid = int(open(pidfile).read())
    except (OSError, ValueError):
        return
    name = proc_table().get(pid, (0, ""))[1].lower()
    if name.startswith("python"):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def cleanup():
    """終了済みセッションの pid ファイル、強制終了で残った WAV、肥大したログを片付ける"""
    import glob, re, time
    alive = proc_table()
    for f in glob.glob(os.path.join(CACHE, "daemon.*.pid")):
        m = re.search(r"daemon\.(\d+)\.pid$", f)
        try:
            daemon = int(open(f).read())
        except (OSError, ValueError):
            daemon = 0
        # Claude Code が終了済み、またはデーモンがもう動いていない (PID 再利用も考慮)
        if m and (int(m.group(1)) not in alive or
                  not alive.get(daemon, (0, ""))[1].lower().startswith("python")):
            try:
                os.remove(f)
            except OSError:
                pass
    for f in glob.glob(os.path.join(CACHE, "tmp*.wav")):
        try:
            if time.time() - os.path.getmtime(f) > 300:   # 再生中のものは消さない
                os.remove(f)
        except OSError:
            pass
    for name in ("daemon.log", "tunnel.log"):
        f = os.path.join(CACHE, name)
        try:
            if os.path.getsize(f) > 1 << 20:              # 1MB を超えたら末尾 256KB だけ残す
                with open(f, "rb") as r:
                    r.seek(-(256 << 10), os.SEEK_END)
                    tail = r.read()
                with open(f, "wb") as w:
                    w.write(tail)
        except OSError:
            pass


def main():
    os.makedirs(CACHE, exist_ok=True)
    try:
        cleanup()
    except Exception:
        pass
    data = json.loads(sys.stdin.buffer.read() or b"{}")
    transcript = data["transcript_path"]
    if WIN:
        ensure_tunnel()
    claude_pid = find_claude_pid()

    pidfile = os.path.join(CACHE, f"daemon.{claude_pid}.pid")
    kill_old_daemon(pidfile)

    log = open(os.path.join(CACHE, "daemon.log"), "ab")
    cmd = [sys.executable, os.path.join(DIR, "zunko_tts.py"),
           "--transcript", transcript, "--watch-pid", str(claude_pid)]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = spawn_bg(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env)
    open(pidfile, "w").write(str(proc.pid))


if __name__ == "__main__":
    main()
