# bridge

the laptop side. claude code hooks and the status line call `tinyclawd.py`, a small daemon owns
the usb serial port and sends [protocol](../firmware/PROTOCOL.md) lines to the pet. many claude
code sessions can run at once, they all go through the same daemon.

```
claude code hooks ──┐
status line ────────┼──> unix socket ──> tinyclawd.py daemon ──> /dev/ttyACM0 ──> pet
send / replay ──────┘
```

needs python 3.8+ and pyserial (only the daemon and `replay --to serial` use it):

```bash
sudo apt install python3-serial      # fedora: python3-pyserial, arch: python-pyserial
```

## install

```bash
python3 setup.py --dry-run   # what it would change, changes nothing
python3 setup.py             # service + hooks + /clawd + usage from the status line
python3 setup.py --port /dev/ttyUSB0   # only if the pet is not found on its own
python3 setup.py --statusline visible  # no status line yet: visible, hidden or none (default: ask)
python3 setup.py --remove    # undo all of it
```

- the service is `~/.config/systemd/user/tinyclawd-daemon.service`. no systemd: it prints the
  command to run the daemon yourself
- hooks go in `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR`). your own hooks stay where they
  are, only entries that run `tinyclawd.py` are touched. running it again fixes the paths, for
  example after you moved the folder. `install_hooks.py` does only this part
- usage: with no status line it asks (in a terminal) or takes `--statusline`. visible =
  `tinyclawd.py statusline`, prints `ctx 37% · 5h 42% · week 18%` with the parts claude code sends.
  hidden = `tinyclawd.py usage`, prints nothing. none = nothing, it prints how to add one later.
  no terminal and no flag counts as none. with a shell script status line it adds this after the
  line that reads stdin (`input=$(cat)`):
  ```bash
  printf '%s' "$input" | /usr/bin/python3 /path/to/bridge/tinyclawd.py usage >/dev/null 2>&1 &
  ```
  anything else, it prints that line for you to add
- settings.json and the status line script get a `.bak-tinyclawd-<time>` copy before a change.
  a bad settings.json is never touched, it tells you instead

the hooks are `async`, so claude never waits for them. `hook` and `usage` never print anything and
`statusline` only prints its line. all three always exit 0, if the daemon is down the event is
just dropped. the one sync hook is `screen` on `UserPromptExpansion`: it only prints for `/clawd`,
to stop that prompt before claude sees it. the daemon is not started by the hooks, run it yourself
or use the service.

## commands

```bash
python3 tinyclawd.py daemon                 # finds the pet by its espressif usb id
python3 tinyclawd.py daemon --port /dev/ttyACM1
python3 tinyclawd.py daemon --stdout        # no board, prints what it would send
python3 tinyclawd.py daemon --no-keys       # do not watch the keyboard
python3 tinyclawd.py send ev perm           # one raw line through the daemon
python3 tinyclawd.py console                # type lines straight to the pet, see its answers
python3 tinyclawd.py focus                  # the terminal of the session waiting for you comes up
python3 tinyclawd.py flash fw.uf2           # flash the xiao rp2040 build, no button needed
python3 tinyclawd.py replay                 # plays ../firmware/sim/scripts/demo.txt
python3 tinyclawd.py replay my.txt --speed 2 --loop --to serial
```

replay goes to the daemon if it is running, else to stdout. `--to serial` opens the port itself,
so stop the daemon first. session files are one line per entry, `<ms> <line>` from the start or
`+<ms> <line>` after the previous one, `#` comments. same format as the simulator.

## what the daemon does

- sends `hello 1` and `time HH:MM` on connect, the time again every 5 min, and the last usage line
- only opens the espressif usb device (or `--port` / `TINYCLAWD_PORT`), never some other serial
  device. logs once why it can't open the port (not plugged in, no permission)
- reconnects when the board is unplugged, and says hello again when the pet prints `boot`. a write
  that hangs for 2 s counts as unplugged, so a stuck board can't freeze it
- ignores everything from the pet that is not `ok` / `err` / `touch` / `boot` (esp-idf logs),
  logs touches to stderr
- keeps state per session: subagent counts are summed over all sessions (`ev sub N`), one
  session's `ev done` or `ev end` does not hide another session that is waiting for you (`ev perm`),
  `ev end` only goes out when the last session ends, usage is only forwarded when it changed or
  every 30 s
- asks `playerctl` every 2 s if something plays (spotify, youtube in a browser...), sends
  `music 1 artist - title` when it starts or the track changes and `music 0` after 5 s of quiet.
  no playerctl, no music mode
- watches how fast you type, so the pet can look at the keyboard while you write a prompt.
  see below
- while music mode is on it also drives the equalizer, see below
- asks logind every 2 s if the screen is locked (`LockedHint`) and sends `away 1` / `away 0`.
  off with `TINYCLAWD_NO_AWAY=1`
- sends `mode <label>` when a session's permission mode changes (shift+tab), or when a session
  starts in a mode other than normal
- a `touch tap` from the pet, or `tinyclawd.py focus`, while a session waits for you brings that
  session's terminal to the front, see below

## showing the waiting session

every hook also sends the pids of claude and the processes above it (shell, terminal). when a
session waits for you, a tap on the pet (or `tinyclawd.py focus`, e.g. on a keyboard shortcut)
looks for a window owned by one of them and activates it. again within 15 s: the next waiting
session.

- kde, wayland or x11: a one-off kwin script over dbus (`gdbus`). in konsole it also switches to
  the right tab
- other x11 desktops: `xdotool`
- gnome on wayland and the rest: no way to raise a window from outside, the tap is just a tap

a terminal multiplexer (tmux, zellij) or another terminal's tabs: the window comes up, the tab
stays where it was.

## typing

the daemon counts key presses in every window. only the count. it has two ways to see them:

- the keyboards in `/dev/input` (wayland, x11 or a tty). it opens every event device that has
  letter keys and no mouse buttons, looks for new ones every 5 s, and counts key-down events
  (repeats and releases are not counted). the key code field is skipped, never read. needs
  the `input` group: `sudo usermod -aG input $USER`, then log out and in. that group lets
  every program you run read the keyboard, so it is your call
- `xinput test-xi2 --root` (x11, no group needed). the key code lines come through the xinput
  pipe but are never parsed, kept, logged or sent, they are dropped as they come in

`/dev/input` wins when a keyboard there can be opened, else xinput on x11. the daemon logs which
one it uses (`typing reactions from /dev/input`). the pet only gets `typing <n>`, n = keys per
second (1 to 15), and `typing 0` after 3 s without keys. at most 2 lines a second, plus the same line every 5 s
while you keep typing.

turn it off:

```bash
TINYCLAWD_NO_KEYS=1 python3 tinyclawd.py daemon
python3 tinyclawd.py daemon --no-keys
```

for the service, add `Environment=TINYCLAWD_NO_KEYS=1` under `[Service]` (or `--no-keys` to the
`ExecStart` line). with no readable keyboard in `/dev/input` and no x11 xinput it is off on its
own, the daemon logs why once. the service often starts before the desktop is up, so without
`/dev/input` access it asks systemd for `DISPLAY` for the first 10 minutes before it gives up.

## equalizer

while something plays, the daemon listens to what the laptop plays (the default output's monitor,
through `parec`, 8 kHz mono), splits it into 8 bands every 80 ms and sends `eq 13579753`, bass
first, at most 12 times a second and only when the levels changed. a slow auto gain makes quiet
and loud songs both fill the bars.

privacy: the sound is analysed on the laptop and thrown away right after. it is never recorded,
never saved and never sent. only the 8 level digits go to the pet, nothing goes to the internet.
the capture only runs while the music watcher says something plays, and stops with `music 0`.

- uses numpy for the fft if `/usr/bin/python3` has it, else a simple loudness + brightness guess
- no `parec` (pulseaudio-utils): no equalizer, logged once, the pet makes up its own bars
- turn it off: `TINYCLAWD_NO_EQ=1` in the daemon's environment (for the service:
  `Environment=TINYCLAWD_NO_EQ=1` under `[Service]`)

## hook mapping

| claude code hook | line |
|---|---|
| SessionStart | `ev start` |
| SessionEnd | `ev end` (when no other session is left) |
| UserPromptSubmit | `ev prompt` |
| PreToolUse | `ev tool <tool_name>`, `AskUserQuestion` / `ExitPlanMode` give `ev perm` |
| PreToolUse, Bash | the command picks the look: `Grep` (grep, rg, find), `Read` (cat, ls, sed -n), `Build` (make, idf.py, cargo build), `Test`, `Git`, `Install` (pip, apt, npm install), `WebFetch` (curl, wget), else `Bash` |
| PermissionRequest | `ev perm` |
| PermissionDenied (auto mode said no) | `ev toolfail <tool_name>` |
| any hook with `permission_mode` | `mode <label>` when it changed (normal mode, accept edits, plan mode, auto mode, dont ask, bypass perms) |
| Notification `permission_prompt`, `elicitation_dialog`, `agent_needs_input`... | `ev perm` |
| PostToolUse, Bash test command | `ev test pass` (or `fail` if the output says so, e.g. piped into tail) |
| PostToolUse, Bash `git commit` with a `[branch hash]` line | `ev commit` |
| PostToolUse with `interrupted` | `ev interrupt` |
| PostToolUseFailure | `ev toolfail <tool_name>`, plus `ev test fail` for test commands |
| PostToolUseFailure with `is_interrupt` | `ev interrupt` |
| Stop / StopFailure | `ev done` |
| SubagentStart / SubagentStop | `ev sub N` (counted by `agent_id`) |
| PreCompact | `ev compact` |

test commands: pytest, `python -m unittest`, tox, nox, npm/pnpm/yarn/bun test, jest, vitest, mocha,
cargo test, go test, ctest, make test/check, meson test, `python test_x.py`. builds (idf.py build)
are not tests.

checked against claude code 2.1.289 (the strings in the binary):

- every hook gets `session_id`, `transcript_path`, `cwd`, `hook_event_name`, and `agent_id` when
  it fires inside a subagent
- `PostToolUseFailure` exists: `tool_name`, `tool_input`, `error`, `is_interrupt`. a Bash command
  with a non zero exit lands here (error starts with `Exit code N`), not in PostToolUse
- Bash `tool_response` in PostToolUse is `{stdout, stderr, interrupted, backgroundTaskId?, ...}`,
  no exit code
- `PermissionRequest` fires when the dialog shows. the `permission_prompt` Notification only comes
  after a delay, so it is a backup
- `SubagentStart` / `SubagentStop` have `agent_id` and `agent_type`
- `Stop` has no interrupt flag and i found no hook for esc while claude is only thinking, so
  `ev interrupt` only shows when esc stops a running tool
- hooks support `"async": true`

## troubleshooting

- nothing happens: is the daemon running? `python3 tinyclawd.py send ping` says "daemon not
  running" if not. socket is `$XDG_RUNTIME_DIR/tinyclawd.sock` (or `/run/user/<uid>/tinyclawd.sock`,
  else `/tmp/tinyclawd-<uid>.sock`)
- which port: the daemon logs `connected /dev/ttyACM0` to stderr (`journalctl --user -u
  tinyclawd-daemon -f` for the service). wrong one: `--port`
- permission denied on the port: you need to be in the group that owns it (`dialout`, `uucp` on
  arch), then log out and in. the daemon log names the group
- `idf.py monitor` and the daemon can not share the port, stop one
- test without the board: `daemon --stdout` in one terminal, `replay` or a hook in another

## tests

```bash
python3 test_bridge.py    # hook mapping, sessions, music, typing, equalizer, a daemon on a pty
python3 test_setup.py     # settings merge and undo, status line, the service, in a throwaway HOME
```

no board needed, and your real `~/.claude` is never read or written.
