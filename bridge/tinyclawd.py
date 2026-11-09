#!/usr/bin/env python3
# tiny clawd bridge: claude code hooks + status line -> usb serial -> pet
#
#   tinyclawd.py daemon [--port P] [--stdout] [--no-keys]   owns the serial port
#   tinyclawd.py hook                           called by claude code hooks (json on stdin)
#   tinyclawd.py usage                          called from the status line (json on stdin)
#   tinyclawd.py statusline                     a status line: prints usage and sends it too
#   tinyclawd.py send "ev perm"                 one raw line
#   tinyclawd.py replay [file] [--speed 2]      play a fake session
#
# only needs pyserial, and only for the daemon / replay --to serial.

import argparse
import ast
import glob
import json
import os
import re
import select
import shlex
import signal
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

PROTO = 1
MAX_LINE = 120
ESPRESSIF_VID = 0x303A
RPI_VID = 0x2E8A        # the xiao rp2040 build, only with the "tinyclawd" product name
HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_FILE = os.path.join(HERE, "..", "firmware", "sim", "scripts", "demo.txt")


def sock_path():
    if os.environ.get("TINYCLAWD_SOCK"):
        return os.environ["TINYCLAWD_SOCK"]
    # hooks and the service must agree even when one of them has no XDG_RUNTIME_DIR
    run = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    if os.path.isdir(run) and os.access(run, os.W_OK):
        return os.path.join(run, "tinyclawd.sock")
    return "/tmp/tinyclawd-%d.sock" % os.getuid()


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def clean(s, n=60):
    # tool names etc go on one short ascii line
    s = re.sub(r"[^\x21-\x7e]", "", str(s))
    return s[:n] or "?"


# ---------------------------------------------------------------- socket client

def send_to_daemon(lines, timeout=0.2, path=None):
    """send lines (str or list) to the daemon. returns False if it is not there."""
    if isinstance(lines, str):
        lines = [lines]
    data = "".join(l.rstrip("\n") + "\n" for l in lines).encode()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(path or sock_path())
        s.sendall(data)
        return True
    except OSError:
        return False
    finally:
        s.close()


def daemon_running(path=None):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(0.2)
        s.connect(path or sock_path())
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------------------------------------------------------------- hook mapping

# matched against the start of each command, after sudo, cd, FOO=1 and pipes are cut off
TEST_RE = re.compile(
    r"(pytest|py\.test|python[\d.]* -m (pytest|unittest)|tox|nox|"
    r"(npm|pnpm|yarn|bun)( run)? test|jest|vitest|mocha|"
    r"cargo (test|nextest)|go test|ctest|make (test|check)|meson test|"
    r"python[\d.]* \S*test\S*\.py"
    r")\b"
)
RUNNERS = {"uv", "poetry", "pipenv", "hatch", "pdm", "npx", "bunx", "pnpx"}   # uv run pytest
GIT_COMMIT_RE = re.compile(r"\bgit(\s+-[cC]\s+\S+)*\s+commit\b")
COMMIT_OUT_RE = re.compile(r"^\[[^\]\n]+ [0-9a-f]{7,}\]", re.M)
FAIL_OUT_RE = re.compile(
    r"(\b\d+ (failed|failing|errors?)\b|^FAILED\b|^FAIL\b|test result: FAILED|^ERRORS?\b)", re.M
)
PERM_NOTES = {
    "permission_prompt", "elicitation_dialog", "elicitation_url_dialog",
    "worker_permission_prompt", "agent_needs_input",
}
ASKING_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
# claude code permission modes -> the label the pet shows when it changes
MODES = {"default": "normal mode", "acceptEdits": "accept edits", "plan": "plan mode",
         "auto": "auto mode", "dontAsk": "dont ask", "bypassPermissions": "bypass perms"}
# slash commands that open a screen on the pet instead of a claude turn
SCREEN_CMDS = {"usage": "usage", "cost": "usage", "status": "usage", "context": "context"}
PET_CMD = "clawd"   # our own slash command: /clawd, /clawd context


# a bash command gets the look of what it really does
SEARCH_CMDS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd", "fdfind"}
READ_CMDS = {"cat", "head", "tail", "less", "more", "bat", "batcat", "wc", "ls", "tree"}
BUILD_CMDS = {"make", "cmake", "ninja", "idf.py", "gcc", "g++", "cc", "c++", "clang", "clang++",
              "tsc", "platformio", "pio", "meson", "scons", "gradle", "mvn"}
WEB_CMDS = {"curl", "wget", "http", "https", "xh"}
PKG_CMDS = {"pip", "pip3", "uv", "apt", "apt-get", "brew", "npm", "pnpm", "yarn", "bun", "cargo",
            "conda", "snap", "gem"}
PKG_SUBS = {"install", "add", "i", "ci", "sync"}
SKIP_CMDS = {"cd", "pushd", "popd", "export", "source", ".", "set", "unset", "true"}
PREFIX_CMDS = {"sudo", "env", "time", "nohup", "exec", "command", "nice", "timeout", "stdbuf"}
SPLIT_OPS = {"&&", "||", ";", "&"}


def words(cmd):
    cmd = cmd.replace("\n", " ; ")   # one command per line
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:   # unbalanced quotes
        return cmd.replace(";", " ; ").replace("&&", " && ").replace("|", " | ").split()


def main_commands(cmd):
    """'cd fw && FOO=1 sudo make -j4 | tail' -> [['make', '-j4']]: one per part, pipes cut off"""
    out, cur, piped, prefix = [], [], False, False
    for w in words(cmd) + [";"]:
        if w in SPLIT_OPS or w == "|":
            if cur and not piped:
                out.append(cur)
            cur, prefix = [], False
            piped = w == "|" or (piped and w not in SPLIT_OPS)
            continue
        if piped:
            continue   # the rest of a pipe only filters what the first part makes
        if not cur:
            if re.match(r"\w+=", w) or w in ("(", ")", "{", "}"):
                continue   # FOO=1, subshells
            if w in PREFIX_CMDS:
                prefix = True
                continue
            if prefix and (w.startswith("-") or re.match(r"\d+[smhd]?$", w)):
                continue   # sudo -E, nice -n 19, timeout 30s
        cur.append(w)
    return [c for c in out if os.path.basename(c[0]) not in SKIP_CMDS]


def is_test(cmd):
    """a test run somewhere in the command. 'pip install pytest' or 'grep pytest' is not one"""
    if not isinstance(cmd, str):
        return False
    for argv in main_commands(cmd):
        argv = [os.path.basename(argv[0])] + argv[1:]
        if argv[0] in RUNNERS and len(argv) > 1:
            argv = argv[2:] if argv[1] in ("run", "exec") else argv[1:]
        if argv and TEST_RE.match(" ".join(argv)):
            return True
    return False


def look_of(argv):
    prog = os.path.basename(argv[0])
    args = argv[1:]
    if re.match(r"python[\d.]*$", prog) and args[:2] == ["-m", "pip"]:
        prog, args = "pip", args[2:]
    sub = args[0] if args else ""
    if prog == "git" or (prog == "gh" and sub != "api"):
        return "Git"
    if prog in WEB_CMDS or prog == "gh":
        return "WebFetch"
    if prog in SEARCH_CMDS:
        return "Grep"
    if prog in READ_CMDS or (prog == "sed" and "-n" in args):
        return "Read"
    if prog in PKG_CMDS and (sub in PKG_SUBS or args[:2] == ["pip", "install"]):
        return "Install"
    if prog in BUILD_CMDS:
        return "Build"
    if prog in ("cargo", "go", "docker", "podman") and sub in ("build", "check"):
        return "Build"
    if prog in ("npm", "pnpm", "yarn", "bun") and any(a.startswith("build") for a in args[:2]):
        return "Build"
    return ""


def bash_look(cmd):
    """the tool name the pet sees for a bash command: Grep, Read, Build, Test, Git, Install,
    WebFetch, or Bash for anything else"""
    if not isinstance(cmd, str) or not cmd.strip():
        return "Bash"
    if is_test(cmd):
        return "Test"
    for argv in main_commands(cmd):
        look = look_of(argv)
        if look:
            return look
    return "Bash"


def tool_look(d):
    tool = d.get("tool_name") or ""
    tin = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
    return bash_look(tin.get("command")) if tool == "Bash" else tool


def slash(d):
    """'/usage now' -> 'usage', from a prompt or a slash command expansion"""
    if d.get("command_name"):
        return str(d["command_name"]).lstrip("/").lower()
    p = (d.get("prompt") or "").strip() if isinstance(d.get("prompt"), str) else ""
    return p[1:].split(None, 1)[0].lower() if p.startswith("/") and len(p) > 1 else ""


def map_hook(d):
    """hook json -> list of protocol lines (can be empty)"""
    ev = d.get("hook_event_name", "")
    tool = d.get("tool_name") or ""
    tin = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
    cmd = tin.get("command") if tool == "Bash" else ""
    cmd = cmd if isinstance(cmd, str) else ""

    if ev == "SessionStart":
        return ["ev start"]
    if ev == "SessionEnd":
        return ["ev end"]
    if ev in ("UserPromptSubmit", "UserPromptExpansion"):
        cmd = slash(d)
        if cmd in SCREEN_CMDS:
            return ["play " + SCREEN_CMDS[cmd]]
        return ["ev prompt"] if ev == "UserPromptSubmit" else []
    if ev == "PreToolUse":
        if tool in ASKING_TOOLS:
            return ["ev perm"]
        return ["ev tool " + clean(tool_look(d))]
    if ev == "PermissionRequest":
        return ["ev perm"]
    if ev == "PermissionDenied":   # auto mode said no
        return ["ev toolfail " + clean(tool)]
    if ev == "Notification":
        if d.get("notification_type") in PERM_NOTES:
            return ["ev perm"]
        return []
    if ev in ("Stop", "StopFailure"):
        return ["ev done"]
    if ev == "PreCompact":
        return ["ev compact"]

    if ev == "PostToolUse":
        r = d.get("tool_response")
        r = r if isinstance(r, dict) else {}
        if r.get("interrupted"):
            return ["ev interrupt"]
        if tool != "Bash" or r.get("backgroundTaskId"):
            return []
        out = "%s\n%s" % (r.get("stdout") or "", r.get("stderr") or "")
        if is_test(cmd):
            return ["ev test fail" if FAIL_OUT_RE.search(out) else "ev test pass"]
        if GIT_COMMIT_RE.search(cmd) and "--dry-run" not in cmd:
            if COMMIT_OUT_RE.search(out) or " -q" in cmd:
                return ["ev commit"]
        return []

    if ev == "PostToolUseFailure":
        if d.get("is_interrupt"):
            return ["ev interrupt"]
        lines = ["ev toolfail " + clean(tool)]
        if tool == "Bash" and is_test(cmd):
            lines.append("ev test fail")
        return lines

    # SubagentStart / SubagentStop are counted by the daemon
    return []


def project(cwd):
    """'/home/me/code/tinyclawd/' -> 'tinyclawd', short ascii for the pet screen"""
    if not isinstance(cwd, str) or not cwd.strip("/"):
        return ""
    return clean(os.path.basename(cwd.rstrip("/")), 16)


def parent_pids(pid=None, depth=12):
    """claude and the processes above it, closest first: [claude, shell, terminal, ...].
    the daemon looks for a window among them when you tap a waiting pet"""
    pids = []
    pid = pid or os.getppid()
    while pid > 1 and len(pids) < depth:
        pids.append(pid)
        try:
            with open("/proc/%d/stat" % pid) as f:
                stat = f.read()
            pid = int(stat[stat.rindex(")") + 2:].split()[1])   # the name can hold spaces
        except (OSError, ValueError, IndexError):
            break
    return pids


def hook_message(d):
    """what the hook sends to the daemon: one json line, the daemon keeps per session state"""
    return json.dumps({
        "sid": str(d.get("session_id") or "?"),
        "ts": time.time(),
        "event": d.get("hook_event_name", ""),
        "aid": d.get("agent_id") or "",
        "lines": map_hook(d),
        "proj": project(d.get("cwd")),
        "tool": clean(tool_look(d), 24) if d.get("tool_name") else "",
        "slash": clean(slash(d), 24) if slash(d) else "",
        "mode": clean(d["permission_mode"], 24) if d.get("permission_mode") else "",
        "pids": parent_pids(),
    })


def screen_reply(d, sent=True):
    """/clawd [usage|context] -> (protocol line, hook json that stops the prompt), else None"""
    if slash(d) != PET_CMD:
        return None
    arg = str(d.get("command_args") or "").strip().lower()
    what = "context" if arg.startswith("context") or arg.startswith("ctx") else "usage"
    reason = "%s is on tiny clawd" % what if sent else "tiny clawd is not running"
    stop = {"decision": "block", "reason": reason,
            "hookSpecificOutput": {"hookEventName": d.get("hook_event_name") or "UserPromptExpansion",
                                   "suppressOriginalPrompt": True}}
    return "play " + what, stop


def cmd_screen(args):
    # runs before a slash command reaches claude. prints json only for /clawd
    try:
        raw = sys.stdin.read()
        d = json.loads(raw) if raw.strip() else {}
        r = screen_reply(d) if isinstance(d, dict) else None
        if r:
            sent = send_to_daemon(r[0], timeout=0.2, path=args.sock)
            print(json.dumps(screen_reply(d, sent)[1]), flush=True)
    except BaseException:
        pass
    os._exit(0)


def cmd_hook(args):
    # stdout of a hook can end up in the session, so never print anything
    try:
        sys.stdout = open(os.devnull, "w")
        raw = sys.stdin.read()
        d = json.loads(raw) if raw.strip() else {}
        if isinstance(d, dict) and d.get("hook_event_name"):
            send_to_daemon(hook_message(d), timeout=0.2, path=args.sock)
    except BaseException:
        pass
    os._exit(0)


# ---------------------------------------------------------------- usage

def dig(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def as_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def reset_minutes(v, now=None):
    """resets_at is unix seconds (the status line does date -d @x). also take ms or iso text."""
    now = time.time() if now is None else now
    t = as_num(v)
    if t is None and isinstance(v, str) and v:
        try:
            from datetime import datetime
            t = datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    if t is None:
        return None
    if t > 1e12:
        t /= 1000.0
    return max(0, int(round((t - now) / 60.0)))


def usage_line(d, now=None):
    parts = []
    for name, keys in (
        ("5h", ("rate_limits", "five_hour", "used_percentage")),
        ("wk", ("rate_limits", "seven_day", "used_percentage")),
        ("ctx", ("context_window", "used_percentage")),
    ):
        v = as_num(dig(d, *keys))
        if v is not None:
            parts.append("%s=%d" % (name, max(0, min(100, round(v)))))
    m = reset_minutes(dig(d, "rate_limits", "five_hour", "resets_at"), now)
    if m is not None:
        parts.append("reset=%d" % m)
    return "usage " + " ".join(parts) if parts else None


def cmd_usage(args):
    try:
        sys.stdout = open(os.devnull, "w")
        d = json.loads(sys.stdin.read() or "{}")
        line = usage_line(d) if isinstance(d, dict) else None
        if line:
            send_to_daemon(line, timeout=0.2, path=args.sock)
    except BaseException:
        pass
    os._exit(0)


def statusline_text(d):
    """'ctx 37% · 5h 42% · week 18%', only the parts claude code sent"""
    parts = []
    for name, keys in (
        ("ctx", ("context_window", "used_percentage")),
        ("5h", ("rate_limits", "five_hour", "used_percentage")),
        ("week", ("rate_limits", "seven_day", "used_percentage")),
    ):
        v = as_num(dig(d, *keys))
        if v is not None:
            parts.append("%s %d%%" % (name, max(0, min(100, round(v)))))
    return " · ".join(parts)


def cmd_statusline(args):
    try:
        d = json.loads(sys.stdin.read() or "{}")
        if not isinstance(d, dict):
            d = {}
        sys.stdout.buffer.write((statusline_text(d) + "\n").encode("utf-8"))
        sys.stdout.flush()
        line = usage_line(d)
        if line:
            send_to_daemon(line, timeout=0.2, path=args.sock)
    except BaseException:
        pass
    os._exit(0)


# ---------------------------------------------------------------- serial link

def find_port():
    """the pet's port by its espressif usb id, or a raspberry pi id named "tinyclawd" (the
    rp2040 build). other serial devices (a flight controller, an arduino, a plain pico) are
    never opened, use --port or TINYCLAWD_PORT for anything unusual"""
    if os.environ.get("TINYCLAWD_PORT"):
        return os.environ["TINYCLAWD_PORT"]
    try:
        from serial.tools import list_ports
        ports = list_ports.comports()
    except Exception:
        ports = None
    if ports is not None:
        return next((p.device for p in ports if p.vid == ESPRESSIF_VID or
                     (p.vid == RPI_VID and p.product == "tinyclawd")), None)
    acm = sorted(glob.glob("/dev/ttyACM*"))   # no usb info at all, best guess
    return acm[0] if acm else None


def port_group(port):
    """dialout on debian and fedora, uucp on arch"""
    try:
        import grp
        return grp.getgrgid(os.stat(port).st_gid).gr_name
    except (OSError, KeyError):
        return "dialout"


class Link:
    """the serial port, reconnecting. with stdout=True it just prints lines."""

    def __init__(self, port=None, stdout=False, on_connect=None, on_line=None):
        self.port = port
        self.stdout = stdout
        self.on_connect = on_connect or (lambda: None)
        self.on_line = on_line or (lambda l: None)
        self.ser = None
        self.lock = threading.Lock()
        self.stop = False
        self.why = None   # last reason the port did not open, logged once

    def connected(self):
        return self.stdout or self.ser is not None

    def send(self, line):
        line = line.strip()[:MAX_LINE]
        if not line:
            return False
        if self.stdout:
            print(line, flush=True)
            return True
        with self.lock:
            if self.ser is None:
                return False
            try:
                self.ser.write((line + "\n").encode("ascii", "replace"))
                return True
            except Exception as e:
                log("write failed:", e)
                self._drop()
                return False

    def _drop(self):
        try:
            self.ser.close()
        except Exception:
            pass
        self.ser = None

    def _open(self):
        import serial
        port = self.port or find_port()
        if not port:
            self.waiting("no pet plugged in (looking for an espressif usb device), waiting")
            return False
        try:
            # write_timeout: a stuck board must not freeze the daemon
            s = serial.Serial(port, 115200, timeout=0.5, write_timeout=2)
        except Exception as e:
            hint = ""
            if "ermission" in str(e):
                hint = ", add yourself to the %s group and log in again" % port_group(port)
            self.waiting("can't open %s: %s%s" % (port, e, hint))
            return False
        with self.lock:
            self.ser = s
        self.why = None
        log("connected", port)
        return True

    def waiting(self, why):
        if why != self.why:
            log(why)
        self.why = why

    def run(self):
        if self.stdout:
            self.on_connect()
            while not self.stop:
                time.sleep(0.5)
            return
        buf = b""
        while not self.stop:
            if self.ser is None:
                if not self._open():
                    time.sleep(2)
                    continue
                buf = b""
                self.on_connect()
            try:
                chunk = self.ser.read(256)
            except Exception as e:
                log("port gone:", e)
                with self.lock:
                    self._drop()
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self.on_line(raw.decode("ascii", "replace").strip())
            if len(buf) > 4096:
                buf = b""


# ---------------------------------------------------------------- daemon state

class Session:
    def __init__(self):
        self.subs = set()
        self.gone = set()
        self.perm = 0.0
        self.done_ts = 0.0
        self.seen = time.time()
        self.busy = False   # between a prompt and Stop
        self.tool = ""      # tool running now, "" = thinking
        self.proj = ""
        self.mode = ""      # permission mode, "" = not heard yet
        self.pids = []      # claude and its parent processes, closest first


class State:
    """per session bookkeeping so many claude code sessions can share one pet"""

    PERM_HOLD = 300      # a pending perm hides other sessions' tool noise this long
    BUSY_TTL = 600       # a busy session this quiet probably lost its Stop hook
    SESSION_TTL = 3600   # forget a silent session (crashed, no SessionEnd)
    USAGE_EVERY = 30

    def __init__(self, out):
        self.out = out   # function(line)
        self.sessions = {}
        self.last_sub = 0
        self.last_busy = 0
        self.last_usage = None
        self.usage_sent = 0.0

    def subs(self):
        return sum(len(s.subs) for s in self.sessions.values())

    def perm_waiting(self, other_than=None, hold=False, now=None):
        now = now or time.time()
        for sid, s in self.sessions.items():
            if sid != other_than and s.perm and (not hold or now - s.perm < self.PERM_HOLD):
                return True
        return False

    def perm_line(self):
        waiting = [s for s in self.sessions.values() if s.perm]
        if len(waiting) >= 2:
            return "ev perm +%d" % len(waiting)
        if waiting and waiting[0].proj:
            return "ev perm " + waiting[0].proj
        return "ev perm"

    def working(self, now, other_than=None):
        """busy sessions not waiting for you, newest first"""
        w = [(s.seen, sid) for sid, s in self.sessions.items()
             if sid != other_than and s.busy and not s.perm and now - s.seen < self.BUSY_TTL]
        return [self.sessions[sid] for _, sid in sorted(w, reverse=True)]

    def busy(self, now):
        return sum(1 for s in self.sessions.values()
                   if s.perm or (s.busy and now - s.seen < self.BUSY_TTL))

    @staticmethod
    def work_line(s):
        return "ev tool " + s.tool if s.tool else "ev prompt"

    def send_subs(self):
        n = self.subs()
        if n != self.last_sub:
            self.last_sub = n
            self.out("ev sub %d" % n)

    def send_busy(self, now):
        # one session is the normal case, the pet only needs to hear about 2+
        n = self.busy(now)
        if n != self.last_busy and max(n, self.last_busy) >= 2:
            self.out("sessions %d" % n)
        self.last_busy = n

    def prune(self, now):
        for sid in [k for k, s in self.sessions.items() if now - s.seen > self.SESSION_TTL]:
            del self.sessions[sid]
        self.send_subs()
        self.send_busy(now)

    def raw(self, line, now=None):
        """a plain protocol line from send / usage / replay"""
        now = now or time.time()
        if line.startswith("usage "):
            if line == self.last_usage and now - self.usage_sent < self.USAGE_EVERY:
                return
            self.last_usage = line
            self.usage_sent = now
        self.out(line)

    def on_mode(self, s, mode):
        """the pet shows the mode for a moment when it changes, or when a session starts
        in a mode other than normal"""
        if not mode or mode == s.mode:
            return
        old, s.mode = s.mode, mode
        if old or mode != "default":
            self.out("mode " + MODES.get(mode, mode)[:16])

    def waiting(self):
        """sessions waiting for you, newest first"""
        w = [(s.perm, sid) for sid, s in self.sessions.items() if s.perm]
        return [self.sessions[sid] for _, sid in sorted(w, reverse=True)]

    def hook(self, m, now=None):
        now = now or time.time()
        self.on_hook(m, now)
        self.send_busy(now)

    def on_hook(self, m, now):
        sid = str(m.get("sid") or "?")
        ev = m.get("event") or ""
        lines = m.get("lines")
        lines = [l for l in lines if isinstance(l, str)] if isinstance(lines, list) else []
        ts = as_num(m.get("ts")) or now
        aid = str(m.get("aid") or "")
        tool = str(m.get("tool") or "")
        if m.get("slash"):
            log("slash command /%s (%s)" % (m["slash"], ev))
        if lines and all(l.startswith("play ") for l in lines):
            for l in lines:
                self.out(l)   # a screen like /usage, not a claude turn
            return

        if ev == "SessionEnd":
            s = self.sessions.pop(sid, None)
            if not self.sessions:
                self.last_sub = 0
                for l in lines:
                    self.out(l)
                return
            self.send_subs()
            if self.perm_waiting():
                self.out(self.perm_line())
            elif s and s.busy and self.working(now):
                self.out(self.work_line(self.working(now)[0]))
            return

        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = Session()
        s.seen = now
        if m.get("proj"):
            s.proj = str(m["proj"])[:16]
        pids = m.get("pids")
        if isinstance(pids, list) and pids and all(isinstance(p, int) for p in pids):
            s.pids = pids[:12]
        self.on_mode(s, str(m.get("mode") or ""))

        if ev == "SubagentStart" and aid and aid not in s.gone:
            s.subs.add(aid)
            self.send_subs()
            return
        if ev == "SubagentStop" and aid:
            s.subs.discard(aid)
            s.gone.add(aid)
            if len(s.gone) > 200:
                s.gone.clear()
            self.send_subs()
            return

        if ev == "PreToolUse" and ts < s.done_ts:
            return   # late async hook from before the turn ended

        was = self.perm_line() if self.perm_waiting() else None
        if ev in ("PermissionRequest", "Notification") and lines:
            s.perm = now
            s.busy = True
            if tool and ev == "PermissionRequest":
                s.tool = tool
        elif ev in ("PostToolUse", "PostToolUseFailure", "UserPromptSubmit",
                    "Stop", "StopFailure", "PermissionDenied"):
            s.perm = 0.0
        if ev == "PreToolUse":
            s.busy = True
            if lines == ["ev perm"]:
                s.perm = now
            elif lines and lines[0].startswith("ev tool "):
                s.tool = lines[0][8:]
        if ev == "UserPromptSubmit":
            s.done_ts = 0.0
            s.busy = True
            s.tool = ""
        asking = "ev perm" in lines
        lines = [self.perm_line() if l == "ev perm" else l for l in lines]

        if ev in ("Stop", "StopFailure"):
            s.done_ts = ts
            s.busy = False
            s.tool = ""
            if self.perm_waiting():
                self.out(self.perm_line())
                return
            others = self.working(now)
            if others:
                # a short party, then straight back to what the others are doing
                self.out("ev done small")
                self.out(self.work_line(others[0]))
                return

        if ev == "PreToolUse" and not asking and self.perm_waiting(hold=True, now=now):
            return   # someone is waiting for you, keep showing that
        for l in lines:
            self.out(l)
        # a test or commit from another session still should not hide a waiting perm
        if lines and ev in ("PostToolUse", "PostToolUseFailure", "PreCompact") \
                and self.perm_waiting(other_than=sid):
            self.out(self.perm_line())
            return

        # the waiting list changed without a line saying so
        now_perm = self.perm_line() if self.perm_waiting() else None
        if ev in ("UserPromptSubmit", "PreToolUse", "Stop", "StopFailure"):
            return
        if was and not now_perm and self.working(now):
            self.out(self.work_line(self.working(now)[0]))
        elif was and now_perm and now_perm != was and now_perm not in lines:
            self.out(now_perm)


# ---------------------------------------------------------------- music

PLAYER_FORMAT = "{{status}}\t{{playerName}}\t{{artist}}\t{{title}}"


def playerctl():
    """one line per player, '' when nothing plays, None when playerctl is not installed"""
    try:
        p = subprocess.run(["playerctl", "-a", "metadata", "--format", PLAYER_FORMAT],
                           capture_output=True, text=True, timeout=2)
    except FileNotFoundError:
        return None
    except (OSError, subprocess.TimeoutExpired):
        return ""
    # exit 1 + "No players found" on stderr when nothing is open. with many players one
    # can fail and still leave good lines, so look at stdout either way
    if p.returncode != 0 and not p.stdout.strip():
        return ""
    return p.stdout


def music_line(text):
    """playerctl output -> 'music 1 artist - title' for the first playing player, else None"""
    for row in (text or "").splitlines():
        f = row.split("\t", 3)
        if len(f) < 4 or f[0] != "Playing":
            continue
        # ascii only, one space between words, so "山下" is just gone
        artist, title = (re.sub(r" +", " ", re.sub(r"[^\x20-\x7e]", "", s)).strip()
                         for s in f[2:4])
        name = "%s - %s" % (artist, title) if artist and title else artist or title
        return ("music 1 " + name[:48]).strip()
    return None


# ---------------------------------------------------------------- equalizer
#
# privacy: while music plays we listen to what the laptop itself plays (the default
# output's monitor) at 8 kHz mono and turn it into 8 band levels right here. the sound
# is never written to disk, never kept past the next 128 ms and never sent anywhere.
# only the 8 digits of `eq 13579753` go to the pet. TINYCLAWD_NO_EQ=1 turns it off.

EQ_RATE = 8000
EQ_BLOCK = 640      # 80 ms of samples per update
EQ_WINDOW = 1024    # fft size, a bit longer for the bass
# band edges in Hz, bass first. roughly octaves, the top is what 8 kHz can hold
EQ_EDGES = (40, 100, 200, 400, 700, 1200, 2000, 3000, 4000)


def eq_disabled():
    return os.environ.get("TINYCLAWD_NO_EQ", "") not in ("", "0")


def parec_cmd(which=None):
    """the capture command, or None when parec is missing"""
    import shutil
    if not (which or shutil.which)("parec"):
        return None
    return ["parec", "-d", "@DEFAULT_MONITOR@", "--format=s16le", "--rate=%d" % EQ_RATE,
            "--channels=1", "--raw", "--latency-msec=40"]


class Capture:
    """parec as a pipe. read() gives raw s16 bytes, b'' when it ended"""

    def __init__(self, cmd):
        self.p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read(self, n):
        return self.p.stdout.read(n)

    def close(self):
        try:
            self.p.kill()
            self.p.wait(timeout=1)
        except Exception:
            pass


def to_samples(raw):
    """s16le bytes -> list of floats -1..1"""
    import array
    a = array.array("h")
    a.frombytes(raw[:len(raw) // 2 * 2])
    if sys.byteorder != "little":
        a.byteswap()
    return [v / 32768.0 for v in a]


class Bands:
    """samples -> 8 levels 0..9, with a slow auto gain so quiet and loud songs both fill the bars"""

    RANGE = 27.0    # dB from an empty bar to a full one, 3 dB a step
    FLOOR = -62.0   # the gain never goes below this, so silence stays at 0
    DECAY = 2.0     # dB per second the gain comes back up after a loud part
    TILT = 2.5      # dB per band, highs carry less energy than bass
    PUNCH = 2.5     # how much a band jumps over its own recent average (kicks, hats)
    HEAD = 6.0      # dB the loudest band sits under full, room for the jumps

    def __init__(self, use_numpy=None):
        if use_numpy is None:
            try:
                import numpy  # noqa: F401
                use_numpy = True
            except ImportError:
                use_numpy = False
        self.numpy = use_numpy
        self.ref = self.FLOOR + self.RANGE   # loudest band seen lately, in dB
        self.avg = None   # each band's recent average, dB
        self.buf = []

    def power_numpy(self, x):
        import numpy as np
        a = np.asarray(x, dtype=np.float32)
        a = a * np.hanning(len(a))
        spec = np.abs(np.fft.rfft(a)) ** 2 / (len(a) ** 2)
        freqs = np.fft.rfftfreq(len(a), 1.0 / EQ_RATE)
        out = []
        for lo, hi in zip(EQ_EDGES, EQ_EDGES[1:]):
            out.append(float(spec[(freqs >= lo) & (freqs < hi)].sum()))
        return out

    def power_plain(self, x):
        # no numpy: loudness from the rms, brightness from how often the wave crosses
        # zero. a hump of bars sits where the sound is. not a real spectrum, but it moves
        n = len(x) or 1
        rms = sum(v * v for v in x) / n
        cross = sum(1 for a, b in zip(x, x[1:]) if (a < 0) != (b < 0))
        hz = cross * EQ_RATE / (2.0 * n)
        centre = 0
        while centre < 7 and EQ_EDGES[centre + 1] < hz:
            centre += 1
        return [rms * 0.25 / (1 + abs(i - centre)) ** 2 for i in range(8)]

    def levels(self, x, dt=0.08):
        """one block of samples -> list of 8 ints 0..9"""
        import math
        self.buf = (self.buf + list(x))[-EQ_WINDOW:]
        p = self.power_numpy(self.buf) if self.numpy else self.power_plain(self.buf)
        db = [10 * math.log10(v + 1e-12) + self.TILT * i for i, v in enumerate(p)]
        # a band only moves a few dB on a beat, so make the jumps bigger
        if self.avg is None:
            self.avg = db
        k = min(1.0, dt / 0.5)
        self.avg = [a + (d - a) * k for a, d in zip(self.avg, db)]
        top = max(db)
        db = [d + self.PUNCH * (d - a) for d, a in zip(db, self.avg)]
        # jumps up to a loud part at once, comes back down slowly
        self.ref = max(self.ref - self.DECAY * dt, top, self.FLOOR + self.RANGE)
        lo = self.ref - self.RANGE + self.HEAD
        out = []
        for d in db:
            v = int(round((d - lo) / self.RANGE * 9))
            out.append(min(9, max(0, v)))
        return out


class Eq:
    """while music plays: capture, analyse, send 'eq 13579753' at most 12 times a second"""

    MIN_GAP = 1 / 12.0

    def __init__(self, out, source=None, bands=None):
        self.out = out   # function(line)
        # source() -> object with read(n) and close(), or None when there is no capture tool
        self.source = source or self.default_source
        self.bands = bands
        self.lock = threading.Lock()
        self.cap = None
        self.on = False
        self.off = eq_disabled()
        self.last_line = None
        self.last_sent = 0.0
        self.thread = None

    @staticmethod
    def default_source():
        cmd = parec_cmd()
        return Capture(cmd) if cmd else None

    def start(self):
        if self.off or self.on:
            return
        cap = self.source()
        if cap is None:
            log("parec not found, no equalizer (the pet fakes the bars)")
            self.off = True
            return
        if self.bands is None:
            self.bands = Bands()
            log("equalizer on,", "numpy fft" if self.bands.numpy else "simple levels (no numpy)")
        with self.lock:
            self.cap = cap
            self.on = True
            self.last_line = None
        self.thread = threading.Thread(target=self.run, args=(cap,), daemon=True)
        self.thread.start()

    def stop(self):
        with self.lock:
            self.on = False
            cap, self.cap = self.cap, None
        if cap:
            cap.close()

    def send(self, levels, now=None):
        """sends the levels if they changed and the last line is old enough"""
        now = time.time() if now is None else now
        line = "eq " + "".join(str(v) for v in levels)
        with self.lock:
            if not self.on or line == self.last_line or now - self.last_sent < self.MIN_GAP:
                return False
            self.last_line = line
            self.last_sent = now
            self.out(line)
            return True

    def run(self, cap):
        while True:
            try:
                raw = cap.read(EQ_BLOCK * 2)
            except Exception:
                raw = b""
            with self.lock:
                if cap is not self.cap:
                    return   # stopped
            if not raw:
                log("audio capture ended, no equalizer until the next song")
                self.stop()
                return
            self.send(self.bands.levels(to_samples(raw)))


class Music:
    """polls playerctl, sends music 1 on start / track change, music 0 after 5 s of silence"""

    EVERY = 2
    OFF_AFTER = 5   # pauses and skips are shorter than this, no flicker

    def __init__(self, out, run=playerctl, eq=None):
        self.out = out   # function(line)
        self.run = run   # function() -> playerctl output
        self.eq = eq     # Eq, started and stopped with the music
        self.line = None   # what the pet shows now, None = no music
        self.last_play = 0.0
        self.off = False
        self.stop = False

    def poll(self, now=None):
        if self.off:
            return
        now = now or time.time()
        text = self.run()
        if text is None:
            log("playerctl not found, no music mode")
            self.off = True
            return
        line = music_line(text)
        if line:
            self.last_play = now
            if line != self.line:
                self.line = line
                self.out(line)
                if self.eq:
                    self.eq.start()
        elif self.line and now - self.last_play >= self.OFF_AFTER:
            self.line = None
            if self.eq:
                self.eq.stop()   # before music 0, so no eq line comes after it
            self.out("music 0")

    def loop(self):
        while not self.stop and not self.off:
            try:
                self.poll()
            except Exception as e:
                log("music:", e)
            time.sleep(self.EVERY)


# ---------------------------------------------------------------- typing

# privacy: we only count that a key went down. which key it was is never read,
# kept, logged or sent, only "typing <keys per second>" leaves this file.
# turn it off with TINYCLAWD_NO_KEYS=1 or daemon --no-keys.
#
# two sources: the keyboards in /dev/input (any session, wayland too, needs the
# input group) and xinput (x11 only, no group needed).

class NoKeys(Exception):
    """no way to watch the keyboard, the message says why"""


SYS_INPUT = "/sys/class/input"
DEV_INPUT = "/dev/input"
EV_KEY = 1
LETTERS = range(16, 26)   # KEY_Q .. KEY_P
BTN_LEFT = 0x110
INPUT_EVENT = struct.Struct("llHHi")   # struct input_event: sec, usec, type, code, value
LONG_BITS = 8 * struct.calcsize("l")
NO_INPUT = ("no access to the keyboard in %s, join the input group: "
            "sudo usermod -aG input $USER, then log out and in" % DEV_INPUT)


def caps(path):
    """a sysfs capability bitmask (hex words, highest first) as one int"""
    try:
        with open(path) as f:
            words = f.read().split()
    except OSError:
        return 0
    bits = 0
    for w in words:
        bits = (bits << LONG_BITS) | int(w, 16)
    return bits


def evdev_keyboards(sys_dir=SYS_INPUT):
    """event devices with letter keys and no mouse buttons, picked by what they can
    send, so mice and combo devices are never opened"""
    found = []
    for name in sorted(os.listdir(sys_dir) if os.path.isdir(sys_dir) else []):
        if not name.startswith("event"):
            continue
        dev = os.path.join(sys_dir, name, "device", "capabilities")
        keys = caps(os.path.join(dev, "key"))
        if (caps(os.path.join(dev, "ev")) >> EV_KEY & 1
                and all(keys >> k & 1 for k in LETTERS) and not keys >> BTN_LEFT & 1):
            found.append(name)
    return found


def evdev_readable(sys_dir=SYS_INPUT, dev_dir=DEV_INPUT):
    for name in evdev_keyboards(sys_dir):
        try:
            os.close(os.open(os.path.join(dev_dir, name), os.O_RDONLY | os.O_NONBLOCK))
            return True
        except OSError:
            pass
    return False


def evdev_keys(on_press, stop=lambda: False, sys_dir=SYS_INPUT, dev_dir=DEV_INPUT, rescan=5.0):
    """reads every keyboard in /dev/input and calls on_press() for every key going down.
    looks for new keyboards every few seconds. raises NoKeys when there are keyboards
    but none can be opened"""
    fds = {}   # fd -> event name
    next_scan = 0.0
    try:
        while not stop():
            now = time.monotonic()
            if now >= next_scan:
                next_scan = now + rescan
                denied = False
                for name in evdev_keyboards(sys_dir):
                    if name in fds.values():
                        continue
                    try:
                        fd = os.open(os.path.join(dev_dir, name), os.O_RDONLY | os.O_NONBLOCK)
                    except PermissionError:
                        denied = True
                        continue
                    except OSError:
                        continue
                    fds[fd] = name
                if denied and not fds:
                    raise NoKeys(NO_INPUT)
            if not fds:
                time.sleep(1.0)
                continue
            ready, _, _ = select.select(list(fds), [], [], 1.0)
            for fd in ready:
                try:
                    data = os.read(fd, INPUT_EVENT.size * 64)
                except BlockingIOError:
                    continue
                except OSError:
                    data = b""
                if not data:   # unplugged
                    os.close(fd)
                    del fds[fd]
                    continue
                for off in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
                    # the code field (which key) is skipped. value 1 = down, 2 = repeat
                    _, _, kind, _, value = INPUT_EVENT.unpack_from(data, off)
                    if kind == EV_KEY and value == 1:
                        on_press()
    finally:
        for fd in fds:
            os.close(fd)


def xinput_keys(on_press):
    """runs xinput until it exits and calls on_press() for every key press.
    raises FileNotFoundError when xinput is not installed."""
    p = subprocess.Popen(["xinput", "test-xi2", "--root"], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, errors="replace")
    try:
        for line in p.stdout:
            # only the event type line counts. the lines after it (device, key code)
            # are dropped right here without looking at them
            if line.startswith("EVENT type") and "(RawKeyPress)" in line:
                on_press()
    finally:
        p.kill()
        p.wait()


SESSION_VARS = ("DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "WAYLAND_DISPLAY")


def session_env(env):
    """the service often starts before the desktop hands DISPLAY to systemd. copy it from
    there, so xinput still works when the daemon came up first"""
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k in SESSION_VARS and not env.get(k):
            env[k] = v


class Keys:
    """counts key presses and sends typing <n> (keys per second, 0 = stopped)"""

    WINDOW = 2.0      # rate over the last 2 s
    QUIET = 3.0       # no keys this long = typing 0
    MIN_GAP = 0.5     # at most 2 lines a second
    SETTLE = 1.5      # a change of 1 waits this long, so it does not flicker
    KEEPALIVE = 5.0   # resend while typing so the pet knows we are still here
    TICK = 0.25
    DISPLAY_WAIT = 600   # look for the desktop this long after start

    def __init__(self, out, source=None, env=None):
        self.out = out          # function(line)
        self.source = source    # function(on_press), blocks while it runs. None = pick()
        self.env = os.environ if env is None else env
        # only the real environment is filled in from systemd (xinput inherits it)
        self.find_session = session_env if env is None else None
        self.can_evdev = evdev_readable
        self.has_xinput = lambda: shutil.which("xinput") is not None
        self.times = []         # when keys went down, nothing else
        self.lock = threading.Lock()
        self.rate = 0           # what the pet was told
        self.sent_at = 0.0
        self.off = False
        self.stop = False

    def why_off(self):
        if self.off:
            return "turned off"
        if self.env.get("TINYCLAWD_NO_KEYS", "") not in ("", "0"):
            return "TINYCLAWD_NO_KEYS is set"
        return None

    def pick(self):
        """/dev/input when a keyboard there can be read, else xinput on x11.
        raises NoKeys with the reason when neither works"""
        if self.can_evdev():
            return evdev_keys
        self.wait_for_display()
        if self.env.get("XDG_SESSION_TYPE") == "wayland" or self.env.get("WAYLAND_DISPLAY"):
            raise NoKeys("wayland session and " + NO_INPUT)
        if not self.env.get("DISPLAY"):
            raise NoKeys("no DISPLAY and " + NO_INPUT)
        if not self.has_xinput():
            raise NoKeys("xinput not found and " + NO_INPUT)
        return xinput_keys

    def press(self, now=None):
        with self.lock:
            self.times.append(now or time.time())

    def tick(self, now=None):
        now = now or time.time()
        with self.lock:
            self.times = [t for t in self.times if now - t < max(self.WINDOW, self.QUIET)]
            last = self.times[-1] if self.times else 0.0
            n = sum(1 for t in self.times if now - t < self.WINDOW)
        raw = n / self.WINDOW
        if not last or now - last >= self.QUIET:
            rate = 0
        else:
            rate = max(1, min(15, int(raw + 0.5)))
        if self.rate and rate and abs(raw - self.rate) < 0.75:
            rate = self.rate   # wobbling around the same speed
        if rate == self.rate:
            if rate and now - self.sent_at >= self.KEEPALIVE:
                self.send(rate, now)
            return
        if rate == 0 or self.rate == 0:
            self.send(rate, now)   # start and stop go out right away
        elif now - self.sent_at >= (self.MIN_GAP if abs(rate - self.rate) >= 2 else self.SETTLE):
            self.send(rate, now)

    def send(self, rate, now):
        self.rate = rate
        self.sent_at = now
        self.out("typing %d" % rate)

    def watch(self):
        """keeps the key source running, waits longer and longer if it keeps dying"""
        wait = 2
        while not self.stop and not self.off:
            start = time.time()
            try:
                self.source(self.press)
            except FileNotFoundError:
                log("xinput not found, no typing reactions")
                self.off = True
                return
            except NoKeys as e:
                log("no typing reactions:", e)
                self.off = True
                return
            except Exception as e:
                log("keys:", e)
            if self.stop:
                return
            wait = 2 if time.time() - start > 30 else min(wait * 2, 60)
            time.sleep(wait)

    def wait_for_display(self):
        start = time.time()
        while not self.env.get("DISPLAY") and self.find_session and not self.stop:
            self.find_session(self.env)
            if self.env.get("DISPLAY") or time.time() - start > self.DISPLAY_WAIT:
                break
            time.sleep(10)

    def loop(self):
        why = self.why_off()
        if why:
            log("typing reactions off:", why)
            return
        if self.source is None:
            try:
                self.source = self.pick()
            except NoKeys as e:
                log("typing reactions off:", e)
                return
            log("typing reactions from", DEV_INPUT if self.source is evdev_keys else "xinput")
        threading.Thread(target=self.watch, daemon=True).start()
        while not self.stop and not self.off:
            try:
                self.tick()
            except Exception as e:
                log("keys:", e)
            time.sleep(self.TICK)


# ---------------------------------------------------------------- focus
#
# a tap while a session waits for you brings its terminal to the front. the hooks send
# the pids of claude and its parents, we look for a window owned by one of them.
# kde (wayland or x11): a short kwin script over dbus, konsole also switches to the tab.
# other x11 desktops: xdotool. anything else: the tap is just a tap.

KWIN_SCRIPT = """
const pids = %s, title = %s;
const list = workspace.windowList ? workspace.windowList() : workspace.clientList();
let best = null, rank = 1e9;
for (const w of list) {
    const i = pids.indexOf(w.pid);
    if (i < 0) continue;
    const r = i * 2 + (title && !w.caption.startsWith(title) ? 1 : 0);
    if (r < rank) { best = w; rank = r; }
}
if (best) {
    if (best.minimized) best.minimized = false;
    if (workspace.windowList) workspace.activeWindow = best; else workspace.activeClient = best;
}
"""


def gdbus(dest, path, method, *args, run=subprocess.run):
    """one dbus call on the session bus, the reply as python values, None on any error"""
    try:
        p = run(["gdbus", "call", "--session", "--dest", dest, "--object-path", path,
                 "--method", method] + [str(a) for a in args],
                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    out = re.sub(r"(?:\b(?:u?int(?:16|32|64)|byte)|@\w+) ", "", p.stdout.strip())
    try:
        return ast.literal_eval(out)
    except (ValueError, SyntaxError):
        return None


def konsole_tab(pids, call=gdbus):
    """switches konsole to the tab running one of these pids. returns the tab title, or ""
    when this is not konsole"""
    for pid in pids:
        dest = "org.kde.konsole-%d" % pid
        windows = konsole_windows(dest, call)
        for win in windows:
            r = call(dest, "/Windows/" + win, "org.kde.konsole.Window.sessionList")
            for sess in (r[0] if r else []):
                r = call(dest, "/Sessions/" + str(sess), "org.kde.konsole.Session.processId")
                if r and r[0] in pids:
                    call(dest, "/Windows/" + win, "org.kde.konsole.Window.setCurrentSession", sess)
                    t = call(dest, "/Sessions/" + str(sess), "org.kde.konsole.Session.title", 1)
                    return t[0] if t else ""
    return ""


def konsole_windows(dest, call=gdbus):
    r = call(dest, "/Windows", "org.freedesktop.DBus.Introspectable.Introspect")
    if not r:
        return []
    return re.findall(r'<node name="(\d+)"', r[0])


def kwin_focus(pids, title="", call=gdbus):
    """runs a one-off kwin script that activates the window. False when kwin is not there"""
    name = "tinyclawd-focus"
    fd, path = tempfile.mkstemp(prefix="tinyclawd-", suffix=".js")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(KWIN_SCRIPT % (json.dumps(pids), json.dumps(title)))
        call("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", name)
        r = call("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.loadScript", path, name)
        if not r or not isinstance(r[0], int) or r[0] < 0:
            return False
        ok = call("org.kde.KWin", "/Scripting/Script%d" % r[0], "org.kde.kwin.Script.run") is not None
        time.sleep(0.3)   # let it run before it goes
        call("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", name)
        return ok
    finally:
        os.unlink(path)


def xdotool_focus(pids, run=subprocess.run):
    for pid in pids:
        try:
            p = run(["xdotool", "search", "--pid", str(pid)], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return False
        wins = p.stdout.split()
        if wins:
            run(["xdotool", "windowactivate", wins[-1]], capture_output=True, timeout=3)
            return True
    return False


def focus_terminal(pids):
    """brings the window running these pids to the front. returns how, or None"""
    title = konsole_tab(pids)
    if kwin_focus(pids, title):
        return "kwin"
    if os.environ.get("DISPLAY") and shutil.which("xdotool") and xdotool_focus(pids):
        return "xdotool"
    return None


# ---------------------------------------------------------------- away
#
# the screen is locked: the pet goes to sleep, and wakes up when you are back.
# asks logind for the graphical session's LockedHint (kde, gnome and friends set it).
# off with TINYCLAWD_NO_AWAY=1.

def loginctl(*args, run=subprocess.run):
    try:
        p = run(["loginctl"] + list(args), capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


class Away:
    EVERY = 2.0

    def __init__(self, out, ask=loginctl):
        self.out = out
        self.ask = ask
        self.sid = None
        self.away = False
        self.stop = False

    def locked(self):
        """True / False, None when logind can't tell"""
        if not self.sid:
            self.sid = self.ask("show-user", str(os.getuid()), "-p", "Display", "--value")
            if not self.sid:
                return None
        v = self.ask("show-session", self.sid, "-p", "LockedHint", "--value")
        if v not in ("yes", "no"):
            self.sid = None   # logged out and in again: a new session next time
            return None
        return v == "yes"

    def tick(self):
        v = self.locked()
        if v is not None and v != self.away:
            self.away = v
            self.out("away %d" % v)

    def loop(self):
        if os.environ.get("TINYCLAWD_NO_AWAY", "") not in ("", "0"):
            log("screen lock watch off: TINYCLAWD_NO_AWAY is set")
            return
        if not shutil.which("loginctl"):
            log("no loginctl, the pet does not know when the screen is locked")
            return
        while not self.stop:
            try:
                self.tick()
            except Exception as e:
                log("away:", e)
            time.sleep(self.EVERY)


# ---------------------------------------------------------------- daemon

DEVICE_PREFIXES = ("ok", "err", "touch", "boot")
FOCUS_CMD = "@focus"   # from `tinyclawd.py focus`, does what a tap does


class Daemon:
    FOCUS_CYCLE = 15.0   # another tap this soon goes to the next waiting session

    def __init__(self, port=None, stdout=False, path=None, player=playerctl, keys=None,
                 no_keys=False, audio=None, focus=focus_terminal):
        self.path = path or sock_path()
        self.link = Link(port, stdout, self.on_connect, self.on_device)
        self.state = State(self.link.send)
        self.eq = Eq(self.link.send, audio)
        self.music = Music(self.link.send, player, self.eq)
        self.keys = Keys(self.link.send, keys)
        self.keys.off = no_keys
        self.away = Away(self.link.send)
        self.focus = focus
        self.focused = (0.0, 0)   # when, and which of the waiting sessions
        self.last_time = 0.0

    def on_connect(self):
        self.link.send("hello %d" % PROTO)
        self.send_time()
        if self.state.last_usage:
            self.link.send(self.state.last_usage)
        if self.state.subs():
            self.link.send("ev sub %d" % self.state.subs())
        if self.state.last_busy >= 2:
            self.link.send("sessions %d" % self.state.last_busy)
        if self.state.perm_waiting():
            self.link.send(self.state.perm_line())
        if self.music.line:
            self.link.send(self.music.line)
        if self.keys.rate:
            self.link.send("typing %d" % self.keys.rate)
        if self.away.away:
            self.link.send("away 1")

    def send_time(self):
        self.last_time = time.time()
        self.link.send("time " + time.strftime("%H:%M"))

    def on_device(self, line):
        word = line.split(" ", 1)[0]
        if word not in DEVICE_PREFIXES:
            return   # esp-idf logs share the port
        if word == "touch":
            log("pet:", line)
            if line == "touch tap":
                self.focus_waiting()
        elif word == "boot":
            log("pet rebooted:", line)
            self.on_connect()
        elif word == "err" or line.startswith("ok tinyclawd"):
            log("pet:", line)

    def focus_waiting(self, now=None):
        """a tap while sessions wait: their terminal comes to the front. tapping again
        soon after goes to the next one"""
        now = now or time.time()
        waiting = [s for s in self.state.waiting() if s.pids]
        if not waiting or not self.focus:
            return
        when, i = self.focused
        i = i + 1 if now - when < self.FOCUS_CYCLE else 0
        self.focused = (now, i)
        s = waiting[i % len(waiting)]
        threading.Thread(target=self.run_focus, args=(s.pids, s.proj), daemon=True).start()

    def run_focus(self, pids, proj):
        try:
            how = self.focus(pids)
        except Exception as e:
            how = None
            log("focus:", e)
        log("tap: %s %s" % ("showing" if how else "no window found for", proj or "the session"))

    def handle_client(self, conn):
        conn.settimeout(0.5)
        data = b""
        try:
            while len(data) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
        finally:
            conn.close()
        for raw in data.decode("utf-8", "replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                if raw == FOCUS_CMD:
                    self.focus_waiting()
                elif raw.startswith("{"):
                    m = json.loads(raw)
                    if isinstance(m, dict):
                        self.state.hook(m)
                else:
                    self.state.raw(raw)
            except Exception as e:
                log("bad message:", e)

    def listen(self):
        if daemon_running(self.path):
            log("already running on", self.path)
            return None   # not an error, so systemd does not keep restarting us
        try:
            os.unlink(self.path)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            srv.bind(self.path)
        finally:
            os.umask(old)
        srv.listen(16)
        return srv

    def run(self, until=None):
        srv = self.listen()
        if srv is None:
            return 0
        log("listening on", self.path)
        t = threading.Thread(target=self.link.run, daemon=True)
        t.start()
        threading.Thread(target=self.music.loop, daemon=True).start()
        threading.Thread(target=self.keys.loop, daemon=True).start()
        threading.Thread(target=self.away.loop, daemon=True).start()
        try:
            while until is None or not until.is_set():
                r, _, _ = select.select([srv], [], [], 1.0)
                if r:
                    try:
                        conn, _ = srv.accept()
                        self.handle_client(conn)
                    except OSError:
                        pass
                now = time.time()
                if self.link.connected() and now - self.last_time > 300:
                    self.send_time()
                self.state.prune(now)
        except KeyboardInterrupt:
            pass
        finally:
            self.link.stop = True
            self.music.stop = True
            self.keys.stop = True
            self.away.stop = True
            self.eq.stop()
            srv.close()
            try:
                os.unlink(self.path)
            except OSError:
                pass
        return 0


def on_term(*a):
    raise KeyboardInterrupt


def cmd_daemon(args):
    if not args.stdout:
        try:
            import serial  # noqa: F401
        except ImportError:
            log("pyserial missing for %s, see the install notes in bridge/README.md" % sys.executable)
            return 2   # the service does not restart on 2, it would only fail again
    # systemd stops us with SIGTERM, clean up the socket like on ctrl-c
    signal.signal(signal.SIGTERM, on_term)
    return Daemon(args.port, args.stdout, args.sock, no_keys=args.no_keys).run()


# ---------------------------------------------------------------- replay

FALLBACK_SESSION = """
# built in fake session, used when firmware/sim/scripts/demo.txt is missing
0 hello 1
+200 usage 5h=42 wk=18 ctx=12 reset=135
+500 ev start
+1500 ev prompt
+3000 ev tool Read
+1500 ev tool Grep
+1500 ev tool Edit
+2000 ev tool Bash
+2500 ev test fail
+1500 ev tool Edit
+1500 ev tool Bash
+2500 ev test pass
+1000 ev tool Task
+500 ev sub 2
+4000 ev sub 1
+2500 ev sub 0
+1000 ev tool Bash
+1500 ev perm
+5000 ev tool Bash
+2000 ev commit
+1500 usage 5h=47 wk=19 ctx=31 reset=128
+500 ev done
+6000 ev prompt
+2000 ev compact
+3000 ev tool WebSearch
+2000 ev toolfail WebFetch
+1500 ev interrupt
+3000 ev end
"""


def parse_script(text):
    """'<ms> line' absolute or '+<ms> line' relative -> [(ms, line)]"""
    out = []
    t = 0
    for n, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split(None, 1)
        if len(parts) < 2:
            raise ValueError("line %d: need '<ms> <line>'" % n)
        stamp, line = parts
        try:
            if stamp.startswith("+"):
                t += int(stamp[1:])
            else:
                t = int(stamp)
        except ValueError:
            raise ValueError("line %d: bad time %r" % (n, stamp))
        out.append((t, line.strip()))
    return out


def cmd_replay(args):
    path = args.file or DEMO_FILE
    if os.path.exists(path):
        text = open(path).read()
        src = path
    elif args.file:
        log("no such file:", path)
        return 1
    else:
        text = FALLBACK_SESSION
        src = "built in session"
    try:
        script = parse_script(text)
    except ValueError as e:
        log(e)
        return 1

    to = args.to or ("daemon" if daemon_running(args.sock) else "stdout")
    link = None
    if to == "serial":
        if daemon_running(args.sock):
            log("the daemon owns the port, use --to daemon (or stop it)")
            return 1
        link = Link(args.port, on_line=lambda l: l.split(" ", 1)[0] in DEVICE_PREFIXES and log("pet:", l))
        try:
            if not link._open():
                log("no pet found, try --port")
                return 1
        except ImportError:
            log("pyserial missing: pip install --user pyserial")
            return 1
        threading.Thread(target=link.run, daemon=True).start()
        link.send("hello %d" % PROTO)
        time.sleep(0.3)

    def out(line):
        if to == "daemon":
            if not send_to_daemon(line, path=args.sock):
                log("daemon not answering")
        elif to == "serial":
            link.send(line)
        else:
            print(line, flush=True)

    speed = args.speed if args.speed > 0 else 1.0
    log("replaying %s (%d lines) to %s" % (src, len(script), to))
    try:
        while True:
            start = time.time()
            for ms, line in script:
                wait = start + ms / 1000.0 / speed - time.time()
                if wait > 0:
                    time.sleep(wait)
                out(line)
            if not args.loop:
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    if link:
        link.stop = True
    return 0


def cmd_console(args):
    """type protocol lines by hand and see every line the pet prints (bring up, tuning)"""
    if daemon_running(args.sock):
        log("the daemon owns the port, stop it first")
        return 1
    try:
        import serial  # noqa: F401
    except ImportError:
        log("pyserial missing: pip install --user pyserial")
        return 1
    link = Link(args.port, on_line=lambda l: l and print("<", l, flush=True))
    threading.Thread(target=link.run, daemon=True).start()
    print("type a line and press enter, ctrl-d to quit. try: hw, hw test 1, hw stats")
    try:
        for line in sys.stdin:
            if line.strip() and not link.send(line):
                log("not connected yet")
    except KeyboardInterrupt:
        pass
    link.stop = True
    return 0


def cmd_send(args):
    line = " ".join(args.line)
    if not send_to_daemon(line, path=args.sock):
        log("daemon not running (%s)" % (args.sock or sock_path()))
        return 1
    return 0


# ---------------------------------------------------------------- flash (rp2040)
#
# the xiao rp2040 takes a .uf2 file: open its port at 1200 baud and it reboots into the
# RPI-RP2 drive, copy the file there and it reboots into the new firmware. no button,
# no root: the drive is mounted through udisks like any usb stick.

UF2_MAGIC = b"UF2\n"
BOOT_LABEL = "RPI-RP2"


def boot_drive():
    """the RPI-RP2 partition's device, or None"""
    p = "/dev/disk/by-label/" + BOOT_LABEL
    return os.path.realpath(p) if os.path.exists(p) else None


def mount_of(dev):
    try:
        with open("/proc/mounts") as f:
            for line in f:
                part = line.split()
                if part[0] == dev:
                    return part[1].replace("\\040", " ")
    except OSError:
        pass
    return None


def wait_for(fn, secs, step=0.25):
    end = time.time() + secs
    while True:
        v = fn()
        if v or time.time() >= end:
            return v
        time.sleep(step)


def mount_boot_drive(dev):
    mp = wait_for(lambda: mount_of(dev), 2)   # a desktop may mount it on its own
    if mp:
        return mp
    try:
        p = subprocess.run(["udisksctl", "mount", "-b", dev, "--no-user-interaction"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError("could not mount %s (%s). mount it, then run this again" % (dev, e))
    if p.returncode != 0:
        raise RuntimeError("could not mount %s: %s" % (dev, (p.stderr or p.stdout).strip()))
    return mount_of(dev) or p.stdout.strip().rsplit(" at ", 1)[-1].rstrip(".")


def flash_uf2(path, port=None, say=print):
    with open(path, "rb") as f:
        if f.read(4) != UF2_MAGIC:
            raise RuntimeError("%s is not a .uf2 file" % path)
    dev = boot_drive()
    if not dev:
        port = port or find_port()
        if not port:
            raise RuntimeError("no tiny clawd found. plug it in, or hold B while plugging it in")
        say("rebooting the board on %s into its usb drive" % port)
        import serial
        try:
            serial.Serial(port, 1200).close()
        except (serial.SerialException, OSError):
            pass   # it often drops off the bus while closing, that is the point
        dev = wait_for(boot_drive, 10)
        if not dev:
            raise RuntimeError("the board did not show up as %s. hold B while plugging it in, "
                               "then run this again" % BOOT_LABEL)
    mp = mount_boot_drive(dev)
    if not os.path.exists(os.path.join(mp, "INFO_UF2.TXT")):
        raise RuntimeError("%s does not look like an rp2040 drive" % mp)
    say("copying %s to %s" % (os.path.basename(path), mp))
    with open(path, "rb") as src, open(os.path.join(mp, "tinyclawd.uf2"), "wb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    if not wait_for(lambda: find_port(), 15):
        raise RuntimeError("copied, but the pet did not come back. unplug it and plug it in again")
    say("flashed, the pet is back")


def cmd_flash(args):
    try:
        flash_uf2(args.file, args.port)
    except (OSError, RuntimeError) as e:
        log(e)
        return 1
    return 0


def cmd_focus(args):
    # for a keyboard shortcut: the terminal of the session waiting for you comes to the front
    if not send_to_daemon(FOCUS_CMD, path=args.sock):
        log("daemon not running (%s)" % (args.sock or sock_path()))
        return 1
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="tinyclawd", description="tiny clawd laptop bridge")
    p.add_argument("--sock", help="unix socket (default %s)" % sock_path())
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daemon", help="own the serial port and forward events")
    d.add_argument("--port", help="serial port, default: auto detect")
    d.add_argument("--stdout", "--dry-run", action="store_true", help="print lines, no serial")
    d.add_argument("--no-keys", action="store_true", help="do not watch the keyboard")
    d.set_defaults(fn=cmd_daemon)

    sub.add_parser("hook", help="claude code hook, json on stdin").set_defaults(fn=cmd_hook)
    sub.add_parser("screen", help="slash command hook for /clawd").set_defaults(fn=cmd_screen)
    sub.add_parser("usage", help="status line json on stdin").set_defaults(fn=cmd_usage)
    sub.add_parser("statusline", help="a visible status line, json on stdin").set_defaults(fn=cmd_statusline)

    s = sub.add_parser("send", help="send one raw protocol line")
    s.add_argument("line", nargs="+")
    s.set_defaults(fn=cmd_send)

    f = sub.add_parser("flash", help="flash a .uf2 onto the xiao rp2040 build, no button needed")
    f.add_argument("file", help="tinyclawd .uf2 file")
    f.add_argument("--port", help="serial port (default: the pet's)")
    f.set_defaults(fn=cmd_flash)

    sub.add_parser("focus", help="show the terminal of the session waiting for you (like a tap)") \
        .set_defaults(fn=cmd_focus)

    c = sub.add_parser("console", help="type lines to the pet, print its answers")
    c.add_argument("--port")
    c.set_defaults(fn=cmd_console)

    r = sub.add_parser("replay", help="play a timed session file")
    r.add_argument("file", nargs="?")
    r.add_argument("--speed", type=float, default=1.0)
    r.add_argument("--to", choices=["serial", "daemon", "stdout"])
    r.add_argument("--port")
    r.add_argument("--loop", action="store_true")
    r.set_defaults(fn=cmd_replay)

    args = p.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
