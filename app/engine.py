#!/usr/bin/env python3
"""NESRecomp Tools engine — UI-free core.

Converts NES ROMs to native PC executables with NESRecomp's cycle backend:
probe (iNES header) -> scaffold -> generate+build (Linux / Windows) ->
verify -> package. Driven by the GUI (app/main.py) or the CLI:

    engine.py toolbox
    engine.py probe --rom game.nes
    engine.py run --job job.json

Job JSON keys:
    rom          path to the .nes file
    title        display title (default: ROM file stem)
    output_dir   where packages + the project folder land
    targets      ["linux"], ["windows"] or both (default both)
    players      1 or 2 (default 2)
    region       "NTSC" or "PAL" (default: from the ROM filename)
    ra           include RetroAchievements (default true)
    ra_popups    show achievement pop-ups / login toast (default true)
    ra_toml      path to a filled retroachievements.toml to ship
    jobs         parallel build jobs (default 2)
    seed_file    optional seed list for problem titles (default null)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
FRAMEWORK_CACHE = APP_DIR / "framework-cache"
NESRECOMP_DIR = FRAMEWORK_CACHE / "nesrecomp"
RECOMP_UI_DIR = FRAMEWORK_CACHE / "recomp-ui"
HOST_BUILD_DIR = NESRECOMP_DIR / "host-build"
ART_CACHE = APP_DIR / "art-cache"

NESRECOMP_GIT = "https://github.com/mstan/nesrecomp.git"
RECOMP_UI_GIT = "https://github.com/RetroPortingToolKit/recomp-ui.git"

LIBRETRO_BOXART_HOSTS = [
    "https://raw.githubusercontent.com/libretro-thumbnails/Nintendo_-_Nintendo_Entertainment_System/master/Named_Boxarts/",
    "https://thumbnails.libretro.com/Nintendo%20-%20Nintendo%20Entertainment%20System/Named_Boxarts/",
]

# Mappers the cycle runtime covered before the additions in MAPPERS.md.
BASE_MAPPERS = {0, 1, 2, 3, 4, 7, 66}


def log_line(log, msg):
    if log:
        log(msg)
    else:
        print(msg, flush=True)


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------

def probe_rom(rom_path):
    """Parse an iNES / NES 2.0 header. Instant; no framework needed."""
    p = Path(rom_path)
    data = p.read_bytes()[:16]
    if len(data) < 16 or data[:4] != b"NES\x1a":
        raise ValueError("not an iNES ROM (bad header)")
    prg_units = data[4]
    chr_units = data[5]
    flags6, flags7, flags8, flags9 = data[6], data[7], data[8], data[9]
    nes2 = (flags7 & 0x0C) == 0x08
    mapper = (flags6 >> 4) | (flags7 & 0xF0)
    submapper = 0
    if nes2:
        mapper |= (flags8 & 0x0F) << 8
        submapper = (flags8 >> 4) & 0x0F
        prg_units |= (flags9 & 0x0F) << 8
        chr_units |= ((flags9 >> 4) & 0x0F) << 8
    mirroring = "four-screen" if (flags6 & 0x08) else ("vertical" if (flags6 & 0x01) else "horizontal")
    stem = p.stem
    region = "NTSC"
    low = stem.lower()
    if "(europe)" in low or "(pal)" in low or "(germany)" in low or "(france)" in low \
            or "(spain)" in low or "(italy)" in low or "(australia)" in low or "(sweden)" in low:
        region = "PAL"
    supported = mapper in supported_mappers()
    return {
        "rom": str(p),
        "title": stem,
        "mapper": mapper,
        "submapper": submapper,
        "prg_kb": prg_units * 16,
        "chr_kb": chr_units * 8,
        "chr_ram": chr_units == 0,
        "mirroring": mirroring,
        "battery": bool(flags6 & 0x02),
        "trainer": bool(flags6 & 0x04),
        "nes2": nes2,
        "region": region,
        "mapper_supported": supported,
        "needs_review": not supported,
        "players": 2,
        "crc32": _rom_crc32(p),
    }


def _rom_crc32(p):
    import zlib
    crc = 0
    with open(p, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
    return "%08x" % (crc & 0xFFFFFFFF)


def supported_mappers():
    """Mapper IDs the cycle runtime covers: the base set plus every ID in
    the additions tables of the framework's runner/cyc/MAPPERS.md."""
    ids = set(BASE_MAPPERS)
    doc = NESRECOMP_DIR / "runner" / "cyc" / "MAPPERS.md"
    if doc.exists():
        try:
            for line in doc.read_text(errors="replace").splitlines():
                m = re.match(r"\|\s*(\d{1,3})\s*\|", line)
                if m:
                    ids.add(int(m.group(1)))
        except OSError:
            pass
    return ids


# --------------------------------------------------------------------------
# Cover art (libretro-thumbnails, both hosts, retries — same policy as the
# PSX app's v1.0.15 fetcher)
# --------------------------------------------------------------------------

def _fetch_bytes_retry(url, tries=3, timeout=20):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nes-recomp-tools"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def _valid_cover(data):
    if not data or len(data) < 8000:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        import struct
        if len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return w >= 200 and h >= 200
        return True
    return data[:3] == b"\xff\xd8\xff"


def fetch_cover(title, log=None):
    """Box art for a ROM title (No-Intro stem). Returns the cached path or
    None. Tries the GitHub mirror first, then the canonical host."""
    ART_CACHE.mkdir(parents=True, exist_ok=True)
    dest = ART_CACHE / (title + ".png")
    if dest.exists() and _valid_cover(dest.read_bytes()):
        return dest
    import urllib.parse
    name = urllib.parse.quote(title + ".png")
    for host in LIBRETRO_BOXART_HOSTS:
        try:
            data = _fetch_bytes_retry(host + name)
        except Exception as e:  # noqa: BLE001
            log_line(log, f"cover: {host.split('/')[2]} failed ({e})")
            continue
        if _valid_cover(data):
            dest.write_bytes(data)
            log_line(log, f"cover: fetched {title}.png ({len(data)} bytes)")
            return dest
        log_line(log, f"cover: {host.split('/')[2]} returned an invalid image")
    log_line(log, f"cover: no box art found for {title}")
    return None


# --------------------------------------------------------------------------
# Toolbox
# --------------------------------------------------------------------------

APT_PACKAGES = {
    "cmake": "cmake",
    "ninja": "ninja-build",
    "gcc": "build-essential",
    "g++": "build-essential",
    "x86_64-w64-mingw32-gcc": "gcc-mingw-w64-x86-64",
    "x86_64-w64-mingw32-g++": "g++-mingw-w64-x86-64",
    "git": "git",
}


def toolbox():
    checks = []
    for tool in ["cmake", "ninja", "gcc", "g++", "git",
                 "x86_64-w64-mingw32-gcc", "x86_64-w64-mingw32-g++"]:
        checks.append({"tool": tool, "found": shutil.which(tool) is not None,
                       "apt": APT_PACKAGES[tool]})
    try:
        out = subprocess.run(["pkg-config", "--modversion", "sdl2"],
                             capture_output=True, text=True, timeout=10)
        sdl2 = out.returncode == 0
    except Exception:  # noqa: BLE001
        sdl2 = False
    checks.append({"tool": "SDL2 dev (pkg-config sdl2)", "found": sdl2,
                   "apt": "libsdl2-dev"})
    checks.append({"tool": "OpenSSL dev (RA HTTPS)",
                   "found": Path("/usr/include/openssl/ssl.h").exists(),
                   "apt": "libssl-dev"})
    py_ok = sys.version_info >= (3, 11)
    checks.append({"tool": f"Python >= 3.11 (have {sys.version.split()[0]})",
                   "found": py_ok, "apt": "python3"})
    missing = sorted({c["apt"] for c in checks if not c["found"]})
    return {"checks": checks, "missing_apt": missing,
            "ready": not missing}


# --------------------------------------------------------------------------
# Frameworks
# --------------------------------------------------------------------------

def ensure_frameworks(log):
    FRAMEWORK_CACHE.mkdir(parents=True, exist_ok=True)
    if not (NESRECOMP_DIR / "runner" / "cyc" / "project.cmake").exists():
        log_line(log, "fetching NESRecomp framework…")
        _git_clone(NESRECOMP_GIT, NESRECOMP_DIR, recursive=True, log=log)
    if not (RECOMP_UI_DIR / "recomp_ui.cmake").exists():
        log_line(log, "fetching recomp-ui…")
        _git_clone(RECOMP_UI_GIT, RECOMP_UI_DIR, recursive=False, log=log)
    _pin_recomp_net(log)
    host = HOST_BUILD_DIR / "NESRecomp"
    if not host.exists():
        log_line(log, "building the host recompiler (one-time)…")
        _run(["cmake", "-S", str(NESRECOMP_DIR / "recompiler"), "-B", str(HOST_BUILD_DIR),
              "-DCMAKE_BUILD_TYPE=Release"], log)
        _run(["cmake", "--build", str(HOST_BUILD_DIR), "-j", "2"], log)
    return host


# NESRecomp pins lib/recomp-net at a commit older than the one the
# current recomp-ui netplay backend compiles against (its
# RNetLobbyModPkg has no 'nf' member). Bump just that submodule to the
# paired commit or every netplay build fails in recomp_netplay_host.c.
RECOMP_NET_PIN = "7a4d330ecf995b881d167234da3c0b01675fd512"


def _pin_recomp_net(log):
    rnet = NESRECOMP_DIR / "lib" / "recomp-net"
    if not (rnet / ".git").exists():
        return
    try:
        cur = subprocess.run(["git", "-C", str(rnet), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        if cur == RECOMP_NET_PIN:
            return
        log_line(log, "pinning lib/recomp-net to the recomp-ui-paired commit…")
        subprocess.run(["git", "-C", str(rnet), "fetch", "--depth", "1",
                        "origin", RECOMP_NET_PIN],
                       capture_output=True, text=True, timeout=600)
        subprocess.run(["git", "-C", str(rnet), "checkout", "-q", RECOMP_NET_PIN],
                       capture_output=True, text=True, timeout=120)
        now = subprocess.run(["git", "-C", str(rnet), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        if now != RECOMP_NET_PIN:
            log_line(log, "WARNING: could not pin lib/recomp-net; "
                          "netplay builds may fail")
    except Exception as exc:  # never block conversions on the pin
        log_line(log, f"WARNING: recomp-net pin failed: {exc}")


def _git_clone(url, dest, recursive, log):
    if dest.exists():
        shutil.rmtree(dest)
    cmd = ["git", "clone", "--depth", "1"]
    if recursive:
        cmd.append("--recursive")
    cmd += [url, str(dest)]
    _run(cmd, log, timeout=1200)


def _run(cmd, log, cwd=None, timeout=None):
    log_line(log, "$ " + " ".join(str(c) for c in cmd))
    proc = subprocess.Popen([str(c) for c in cmd], cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    try:
        for line in proc.stdout:
            log_line(log, line.rstrip())
    finally:
        proc.wait(timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(str(c) for c in cmd)}")
    return proc.returncode


# --------------------------------------------------------------------------
# Project scaffold / build / verify / package
# --------------------------------------------------------------------------

def _slug(title):
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "nes-game"


def _camel(title):
    parts = re.sub(r"[^A-Za-z0-9 ]", " ", title).split()
    name = "".join(p[:1].upper() + p[1:] for p in parts)
    return name or "NesGame"


TOOLCHAIN_MINGW = """set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_C_COMPILER x86_64-w64-mingw32-gcc)
set(CMAKE_CXX_COMPILER x86_64-w64-mingw32-g++)
set(CMAKE_RC_COMPILER x86_64-w64-mingw32-windres)
set(CMAKE_FIND_ROOT_PATH /usr/x86_64-w64-mingw32)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
"""

CMAKELISTS_TMPL = """cmake_minimum_required(VERSION 3.20)
project({camel} C CXX)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
include("{nes_dir}/runner/cyc/project.cmake")
nesrecomp_add_cycle_game({camel}
    ROM "${{CMAKE_SOURCE_DIR}}/game.nes"
    GAME_CONFIG "${{CMAKE_SOURCE_DIR}}/game-cycle.toml"
    GAME_ID "{slug}"
    PLAYERS {players}
    REGION {region}
    RECOMPILER "${{NES_TOOLS_HOST_RECOMPILER}}"{seed_line}{extras_line}{boxart_line})
# Rollback netplay (recomp-net): session facade, ROM identity and the
# launcher lobby adapter. Without this call the launcher has no NETPLAY
# button and the host's netplay paths compile out entirely.
nesrecomp_enable_recomp_net({camel})
{ra_block}"""

RA_CMAKE_BLOCK = """
# RetroAchievements module (bundled): HOST_EXTRAS hooks + rcheevos.
file(GLOB_RECURSE RCHEEVOS_SOURCES
    "${CMAKE_CURRENT_SOURCE_DIR}/ra/ra-rcheevos/src/*.c")
list(FILTER RCHEEVOS_SOURCES EXCLUDE REGEX "rc_libretro\\\\.c$")
list(FILTER RCHEEVOS_SOURCES EXCLUDE REGEX "rc_client_raintegration\\\\.c$")
target_sources({camel} PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/ra/ra_http.c"
    ${RCHEEVOS_SOURCES})
target_include_directories({camel} PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/ra/ra-rcheevos/include"
    "${CMAKE_CURRENT_SOURCE_DIR}/ra")
if(WIN32)
    target_link_libraries({camel} PRIVATE winhttp)
else()
    find_package(OpenSSL REQUIRED)
    target_link_libraries({camel} PRIVATE OpenSSL::SSL OpenSSL::Crypto)
endif()
{popups_def}"""

README_TMPL = """{title} — native PC recompilation (NESRecomp, cycle backend)

HOW TO PLAY
  Run {exe}. The launcher opens first: choose your ROM with
  "Browse For ROM" and press PLAY. The game only accepts the exact
  ROM it was built from (it checks the ROM's fingerprint), which is
  the .nes file you converted. Your ROM is not included — supply
  your own dump of your own cartridge.

CONTROLS
  Keyboard and gamepads are configured in the launcher
  (Player 1 / Player 2 tabs). In game: F7 opens the save-state
  browser, F8 rewinds (when enabled in Settings).

NETPLAY
  Two-player games can be played online from the launcher's
  Netplay page: set a player name, host or join a lobby. Both
  players must use the identical ROM. LAN lobbies are discovered
  automatically.

RETROACHIEVEMENTS
{ra_block}
CREDITS
  Built with NESRecomp by Matthew Stanley (mstan), part of the
  RetroPortingToolkit (retroportingtoolkit.com;
  github.com/mstan, github.com/RetroPortingToolKit).
  Launcher: recomp-ui (MIT, (c) 2026 Matthew Stanley).
"""

RA_BLOCK_ON = """  This build includes RetroAchievements. Put your
  retroachievements.toml next to the executable (username and
  api_token — the Connect token, in double quotes; hardcore
  unquoted) and restart the game. Unlocks register as
  Casual/softcore: RetroAchievements counts hardcore only from
  approved clients, and recomp clients are not one yet."""

RA_BLOCK_OFF = "  This build was made with RetroAchievements off."


def scaffold_project(job, probe, host_recompiler, log):
    out_dir = Path(job["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    title = job.get("title") or probe["title"]
    camel = _camel(title)
    slug = _slug(title)
    proj = out_dir / (camel + "-src")
    proj.mkdir(parents=True, exist_ok=True)
    shutil.copy2(job["rom"], proj / "game.nes")
    # [game] name becomes cyc_native_display_name: the launcher's
    # header, the window title and the netplay game name. Without it
    # the launcher falls back to the generated program name ("game").
    toml_name = title.replace("\\", "\\\\").replace('"', '\\"')
    (proj / "game-cycle.toml").write_text(
        '[game]\nname = "%s"\noutput_prefix = "%s"\ncycle_accurate = true\n'
        % (toml_name, slug))
    seed_line = ""
    if job.get("seed_file"):
        shutil.copy2(job["seed_file"], proj / "seeds.txt")
        seed_line = '\n    SEED_FILE "${CMAKE_SOURCE_DIR}/seeds.txt"'
    extras_line = ""
    ra_block = ""
    if job.get("ra", True):
        ra_src = APP_DIR / "bundle" / "ra"
        if not (ra_src / "ra_extras.cpp").exists():
            raise RuntimeError("RA bundle missing (bundle/ra/ra_extras.cpp) — "
                               "build with ra=false or install the bundle")
        shutil.copytree(ra_src, proj / "ra", dirs_exist_ok=True)
        extras_line = '\n    HOST_EXTRAS "${CMAKE_SOURCE_DIR}/ra/ra_extras.cpp"'
        popups_def = "" if job.get("ra_popups", True) else \
            'target_compile_definitions(%s PRIVATE RA_DISABLE_POPUPS=1)' % camel
        ra_block = RA_CMAKE_BLOCK.replace("{camel}", camel) \
                                 .replace("{popups_def}", popups_def)
    boxart_line = ""
    cover = fetch_cover(title, log)
    if cover:
        shutil.copy2(cover, proj / "boxart.png")
        boxart_line = '\n    BOXART "${CMAKE_SOURCE_DIR}/boxart.png"'
    (proj / "CMakeLists.txt").write_text(CMAKELISTS_TMPL.format(
        camel=camel, nes_dir=str(NESRECOMP_DIR).replace("\\", "/"),
        slug=slug, players=job.get("players", probe.get("players", 2)),
        region=job.get("region", probe["region"]),
        seed_line=seed_line, extras_line=extras_line, boxart_line=boxart_line,
        ra_block=ra_block))
    (proj / "tc-mingw.cmake").write_text(TOOLCHAIN_MINGW)
    return proj, camel, title


def build_target(proj, target, jobs, host_recompiler, log):
    if target == "linux":
        bdir = proj / "build-linux"
        cmd = ["cmake", "-S", str(proj), "-B", str(bdir),
               "-DCMAKE_BUILD_TYPE=Release",
               "-DNESRECOMP_RECOMP_UI=" + str(RECOMP_UI_DIR)]
    else:
        bdir = proj / "build-win"
        cmd = ["cmake", "-S", str(proj), "-B", str(bdir),
               "-DCMAKE_BUILD_TYPE=Release",
               "-DCMAKE_TOOLCHAIN_FILE=" + str(proj / "tc-mingw.cmake"),
               "-DNES_TOOLS_HOST_RECOMPILER=" + str(host_recompiler),
               "-DNESRECOMP_RECOMP_UI=" + str(RECOMP_UI_DIR)]
    _run(cmd, log, timeout=1800)
    _run(["cmake", "--build", str(bdir), "-j", str(jobs)], log, timeout=3600)
    exe = bdir / (proj.name.replace("-src", "") + (".exe" if target == "windows" else ""))
    return bdir, exe


def verify_build(bdir, exe):
    if not exe.exists() or exe.stat().st_size < 1_000_000:
        return {"ok": False, "reason": f"binary missing or too small: {exe}"}
    gen = list(bdir.glob("cycle-*/**/sources.cmake"))
    if not gen:
        return {"ok": False, "reason": "generated sources not found"}
    return {"ok": True, "size": exe.stat().st_size}


def _mingw_dll(name):
    out = subprocess.run(["x86_64-w64-mingw32-g++", "-print-file-name=" + name],
                         capture_output=True, text=True, timeout=15)
    p = Path(out.stdout.strip())
    return p if p.exists() else None


def package(job, probe, camel, title, target, exe, log):
    out_dir = Path(job["output_dir"])
    stage = out_dir / (camel + "-pkg-" + target)
    if stage.exists():
        shutil.rmtree(stage)
    game_dir = stage / camel
    game_dir.mkdir(parents=True)
    exe_name = camel + (".exe" if target == "windows" else "")
    shutil.copy2(exe, game_dir / exe_name)
    # The launcher loads its runtime assets (fonts, verdict icons, and
    # the game's box art as assets/img/boxart.tga) from an assets/ folder
    # beside the exe. Without it the launcher falls back to a placeholder
    # cart image — the box art only shows when assets/ ships.
    assets_src = exe.parent / "assets"
    if assets_src.is_dir():
        shutil.copytree(assets_src, game_dir / "assets")
    if target == "windows":
        sdl = NESRECOMP_DIR / "runner" / "external" / "SDL2" / "SDL2.dll"
        if sdl.exists():
            shutil.copy2(sdl, game_dir / "SDL2.dll")
        for dll in ("libgcc_s_seh-1.dll", "libstdc++-6.dll"):
            src = _mingw_dll(dll)
            if src:
                dst = game_dir / dll
                shutil.copy2(src, dst)
                try:  # the MinGW runtime DLLs ship with full symbols; strip
                    subprocess.run(["x86_64-w64-mingw32-strip", str(dst)],
                                   capture_output=True, timeout=60)
                except Exception:  # noqa: BLE001
                    pass
    ra_on = job.get("ra", True)
    if ra_on and job.get("ra_toml") and Path(job["ra_toml"]).exists():
        shutil.copy2(job["ra_toml"], game_dir / "retroachievements.toml")
    cover = ART_CACHE / (title + ".png")
    if cover.exists():
        art_dir = game_dir / "artwork" / title
        art_dir.mkdir(parents=True)
        shutil.copy2(cover, art_dir / "front.png")
    (game_dir / "README.txt").write_text(README_TMPL.format(
        title=title, exe=exe_name,
        ra_block=RA_BLOCK_ON if ra_on else RA_BLOCK_OFF))
    zip_path = out_dir / (camel + ("-Windows.zip" if target == "windows" else "-Linux.zip"))
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(game_dir.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(stage))
    shutil.rmtree(stage)
    log_line(log, f"package: {zip_path} ({zip_path.stat().st_size} bytes)")
    return zip_path


def run_job(job, log=None):
    probe = probe_rom(job["rom"])
    job.setdefault("region", probe["region"])
    job.setdefault("players", probe.get("players", 2))
    log_line(log, f"probe: mapper {probe['mapper']} PRG {probe['prg_kb']}K "
                  f"CHR {probe['chr_kb']}K {probe['mirroring']} region {probe['region']} "
                  f"supported={probe['mapper_supported']}")
    if probe["needs_review"] and not job.get("force"):
        log_line(log, f"mapper {probe['mapper']} is not in the cycle runtime's "
                      "coverage list — flagged needs-review; set force to try anyway")
        return {"status": "needs_review", "probe": probe}
    host = ensure_frameworks(log)
    proj, camel, title = scaffold_project(job, probe, host, log)
    results = {}
    for target in job.get("targets", ["linux", "windows"]):
        log_line(log, f"building {target}…")
        bdir, exe = build_target(proj, target, int(job.get("jobs", 2)), host, log)
        v = verify_build(bdir, exe)
        if not v["ok"]:
            raise RuntimeError(f"verify failed for {target}: {v['reason']}")
        log_line(log, f"verify {target}: ok ({v['size']} bytes)")
        results[target] = str(package(job, probe, camel, title, target, exe, log))
    return {"status": "done", "probe": probe, "packages": results}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "toolbox":
        print(json.dumps(toolbox(), indent=2))
        return 0
    if cmd == "probe":
        rom = argv[argv.index("--rom") + 1]
        print(json.dumps(probe_rom(rom), indent=2))
        return 0
    if cmd == "run":
        job_path = argv[argv.index("--job") + 1]
        job = json.loads(Path(job_path).read_text())
        result = run_job(job)
        print(json.dumps({"RESULT": result["status"], **result}, indent=2))
        return 0 if result["status"] == "done" else 1
    print("unknown command: " + cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
