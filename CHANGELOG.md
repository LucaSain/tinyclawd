# changelog

## unreleased

- the xiao rp2040 build flashes from the bridge (`tinyclawd.py flash`) and from `install.sh`,
  no button needed. github actions builds both firmwares and attaches them to tagged releases
- the pet falls asleep while the laptop screen is locked and wakes up when you are back
- a tap while a session waits for you brings its terminal to the front (kde: konsole tab too,
  x11: xdotool). `tinyclawd.py focus` does the same from a keyboard shortcut
- permission mode changes show as a small tab at the top. auto mode denials count as a tool error

## v1.0.0 (2026-10-17)

first public version.

- firmware for the ESP32-C3 + SH1106 OLED: 107 animations, 7 games, one touch pad
- reacts to claude code: thinking, every tool, subagents, permission prompts, tests, commits, done
- usage limits and context on its face, read from the status line
- several claude code sessions share one Clawd
- music mode with the song name and an equalizer from the laptop audio
- typing reactions (key speed only)
- laptop bridge: hooks, status line and a background service, set up with one command
- `/clawd` and `/clawd context` show usage or context on the pet without a claude turn
- one line installer, and a single .bin to flash from the browser
- print files for the Clawd body, back plate and stands, plus a small Mac version
- desktop simulator that runs the same pet engine
- site at tinyclawd.deepakpopli.in
