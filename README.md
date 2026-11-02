# NESRecomp Tools

Convert your own NES ROM dumps into native PC games — no emulator.
Each game's code is recompiled with **NESRecomp** (cycle-accurate
backend) into a real executable with a launcher, save states, rewind,
rollback netplay, cover art, and RetroAchievements with in-game
pop-ups.

## Run it

- Linux: `./nes-recomp-tools.sh`
- Windows: `nes-recomp-tools.bat`

First run sets up a small Python environment automatically. On Linux
the app can install the system build packages it needs (compiler,
CMake, SDL2/OpenSSL dev files, MinGW for Windows builds) with one
admin prompt — the Toolbox line at the bottom tells you what is
missing.

## Use it

1. **Add ROMs…** (or a whole folder). Each `.nes` is probed instantly
   from its header: mapper, PRG/CHR sizes, mirroring, region.
2. Check the Settings: RetroAchievements and pop-ups on/off, an RA
   toml to bundle into every package, build targets, output folder.
3. **Run Queue.** Games convert one at a time; a failure is marked
   red and the queue moves on. Packages land in the output folder as
   `<Game>-Linux.zip` / `<Game>-Windows.zip`.

ROMs whose mapper is outside the runtime's coverage list are flagged
**needs review** and skipped (Settings has a "try anyway" override).

## The packages

A package holds the game executable, its `assets/` folder (launcher
art, fonts, icons — keep it next to the exe), a README, and — when RA
is on and you provided one — your `retroachievements.toml`. It does
**not** contain the ROM: on first launch you point the launcher at
your own `.nes` file and it verifies the fingerprint matches the ROM
the game was built from.

## Credits

Built on **NESRecomp by Matthew Stanley (mstan)**, part of the
**RetroPortingToolkit** (retroportingtoolkit.com;
github.com/mstan, github.com/RetroPortingToolKit). NESRecomp is
licensed under the PolyForm Noncommercial License 1.0.0. The
launcher is **recomp-ui** (MIT, © 2026 Matthew Stanley).

Your ROMs stay yours: nothing about them leaves your machine except
the box-art lookup (the game's title) and, when you play with RA
enabled, the usual RetroAchievements traffic from the game itself.
