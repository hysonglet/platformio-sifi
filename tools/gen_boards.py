#!/usr/bin/env python3
"""
Generate PlatformIO board definitions for every board target shipped by the
SiFli SDK, i.e. every ``customer/boards/<board>`` that has an ``hcpu`` or
``lcpu`` sub-directory.

Usage:
    python3 tools/gen_boards.py [--sdk <SiFli-SDK>] [--out <boards-dir>] [--dry-run]

Notes
-----
* ``build.board`` is the only field the platform builder consumes: see
  ``builder/frameworks/sifli.py::_resolve_board``, which passes it to the SDK
  as ``scons --board=<build.board>[_hcpu]``.  Everything else in the JSON is
  metadata for the IDE, upload size checks and debuggers.
* ``debug.jlink_device`` comes from the board's ``<core>/rtconfig.py``
  (``JLINK_DEVICE``).
* ``flash_size`` is taken from the board naming convention (``n<X>``/``e<X>`` =
  MB of NOR flash), then from ``doc/index.md`` (module name ``...-N16R8``), and
  only as a last resort from the partition table top (which may under-report,
  because the partition table does not have to cover the whole flash).
* ``ram_size`` is the SoC's HPSYS SRAM total; it does not depend on the board.
* ``upload.maximum_size`` is the ``HCPU_FLASH_CODE`` region size taken from the
  board's ``ptab.json`` (the same number the SDK uses).
"""

import argparse
import json
import os
import re
import sys

# Directories under customer/boards that are not selectable board targets:
# *_base dirs are shared BSP bases (no hcpu/lcpu dir, filtered anyway),
# `dummy` is a scaffold and `pc` is the PC simulator.
EXTRA_SKIP = {"dummy", "pc"}

# SoC information.  f_cpu is informational: the real clock tree is configured by
# the SDK (customer/boards/*/bsp_init.c); 120/240 MHz are the values the SiFli
# devkits run at.
CHIP_INFO = {
    "SF32LB52X": {"mcu": "sf32lb52x", "f_cpu": "120000000L", "sram_kb": 512},
    "SF32LB55X": {"mcu": "sf32lb55x", "f_cpu": "240000000L", "sram_kb": 1024},
    "SF32LB56X": {"mcu": "sf32lb56x", "f_cpu": "240000000L", "sram_kb": 800},
    "SF32LB58X": {"mcu": "sf32lb58x", "f_cpu": "240000000L", "sram_kb": 1024},
}

# Boards whose flash size is documented but not encoded in their name, doc/ or
# board.conf.  Value = MB of NOR flash, source noted in the comment; extend this
# table if a board shows up as "flash=unknown" in the generator output.
FLASH_SIZE_OVERRIDES = {
    # LCKFB "Huangshan Pai" (SF32LB52-ULP) kit, module SF32LB52-MOD-1-N16R8
    # (see https://wiki.sifli.com/board/sf32lb52x/SF32LB52-%E9%BB%84%E5%B1%B1%E6%B4%BE.html)
    "sf32lb52-lchspi-ulp": 16,
}



def resolve_flash_size(board, board_dir, core):
    """Return (size_mb, source) for the board's NOR flash capacity.

    Priority:
      1. FLASH_SIZE_OVERRIDES (documented values for boards whose name/doc/
         board.conf don't carry the number, e.g. the Huangshan Pai kit)
      2. the board name (n16r8, e8r16, core_n4, ...)
      3. doc/*.md module names (SF32LB52-MOD-1-N16R8)
      4. board.conf CONFIG_BSP_QSPI1_MEM_SIZE (the MPI1 NOR flash size)
      5. doc/*.md NAND modules (SF32LB52-MOD-1-A128R16) - a different memory
         type, so this only wins when nothing else is known
    """
    if board in FLASH_SIZE_OVERRIDES:
        return FLASH_SIZE_OVERRIDES[board], "table"

    mb = name_flash_size_mb(board)
    if mb is not None:
        return mb, "name"

    mb, source = doc_flash_size_mb(board_dir)
    if mb is not None and source == "doc(nor)":
        return mb, source

    mb = board_conf_flash_size_mb(board_dir, core)
    if mb is not None:
        return mb, "board.conf"

    mb, source = doc_flash_size_mb(board_dir)
    if mb is not None:
        return mb, source
    return None, "unknown"


def read_rtconfig(path):
    """Return a dict of the simple ``NAME = 'value'`` assignments of rtconfig.py."""
    fields = {}
    if not os.path.isfile(path):
        return fields
    with open(path, "r", errors="ignore") as f:
        for line in f:
            m = re.match(r"\s*([A-Z_]+)\s*=\s*['\"]([^'\"]+)['\"]", line)
            if m:
                fields[m.group(1)] = m.group(2)
    return fields


def ptab_main_region_size(path):
    """Size (bytes) of the code region, or None.

    The application code lives in ``HCPU_FLASH_CODE``; on boards that execute
    from RAM/PSRAM it is ``HCPU_PSRAM_CODE`` instead.  The region may sit in any
    of the flash1..flash5 devices, so every device is searched.
    """
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", errors="ignore") as f:
            ptab = json.load(f)
    except Exception:
        return None
    for tag in ("HCPU_FLASH_CODE", "HCPU_PSRAM_CODE"):
        for mem in ptab:
            for region in mem.get("regions", []):
                if tag in region.get("tags", []):
                    return int(region["max_size"], 16)
    return None


def doc_flash_size_mb(board_dir):
    """Read the module name from doc/*.md, e.g. SF32LB52-MOD-1-N16R8 / -A128R16.

    Returns (size_mb, source) where source is 'doc(nor)' for the N... modules or
    'doc(nand)' for the A... modules.
    """
    doc_dir = os.path.join(board_dir, "doc")
    if not os.path.isdir(doc_dir):
        return None, "unknown"
    for name in sorted(os.listdir(doc_dir)):
        if not name.endswith(".md"):
            continue
        with open(os.path.join(doc_dir, name), "r", errors="ignore") as f:
            text = f.read()
        m = re.search(r"-N(\d+)R\d+", text)          # NOR flash module
        if m:
            return int(m.group(1)), "doc(nor)"
        m = re.search(r"-A(\d+)R\d+", text)          # NAND flash module
        if m:
            return int(m.group(1)), "doc(nand)"
    return None, "unknown"


def board_conf_flash_size_mb(board_dir, core):
    """MB of the main NOR flash from board.conf (CONFIG_BSP_QSPI1_MEM_SIZE)."""
    conf = os.path.join(board_dir, core, "board.conf")
    if not os.path.isfile(conf):
        return None
    with open(conf, "r", errors="ignore") as f:
        for line in f:
            m = re.match(r"\s*CONFIG_BSP_QSPI1_MEM_SIZE\s*=\s*(\d+)", line)
            if m:
                return int(m.group(1))
    return None


def name_flash_size_mb(board):
    """MB of NOR flash encoded in the board name.

    SiFli naming: ``n<X>r<Y>`` = X MB NOR + Y MB PSRAM (``n16r8``,
    ``n16r32n1``), ``e<X>r<Y>`` = embedded NOR (``e8r16``), ``a<X>r<Y>`` = the
    module carries X MB of NAND instead of NOR.  The trailing ``n<Z>`` of names
    like ``n16r32n1`` is the NAND capacity in Gb, not a NOR size, so a bare
    ``n<X>`` is only used when the board is not a NAND (``a...``) variant.
    """
    m = re.search(r"(?:^|[^a-z0-9])n(\d+)r\d+", board)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:^|[^a-z0-9])e(\d+)r\d+", board)
    if m:
        return int(m.group(1))
    if re.search(r"(?:^|[^a-z0-9])a\d+", board):     # NAND module: let doc decide
        return None
    m = re.search(r"(?:^|[_-])n(\d+)$", board)       # core_n4 / nano_n4
    if m:
        return int(m.group(1))
    return None


def gen_board(sdk, board):
    board_dir = os.path.join(sdk, "customer", "boards", board)
    if not os.path.isdir(board_dir):
        return None

    core = None
    for candidate in ("hcpu", "lcpu", "acpu"):
        if os.path.isdir(os.path.join(board_dir, candidate)):
            core = candidate
            break
    if core is None:                      # shared *_base BSP, not a target
        return None

    rt = read_rtconfig(os.path.join(board_dir, core, "rtconfig.py"))
    chip = (rt.get("CHIP") or "").upper()
    info = CHIP_INFO.get(chip, {})
    mcu = info.get("mcu") or chip.lower()

    flash_mb, source = resolve_flash_size(board, board_dir, core)
    ptab = os.path.join(board_dir, "ptab.json")

    main_size = ptab_main_region_size(ptab)
    sram_kb = info.get("sram_kb")

    build_board = board if core == "hcpu" else "%s_%s" % (board, core)

    data = {
        "platform": "sifli",
        "build": {
            "board": build_board,
            "core": core,
            "mcu": mcu,
            "cpu": "cortex-m33",
            "framework": "sifli-sdk",
        },
        "upload": {
            "protocol": "uart",
            "protocols": ["uart", "jlink", "cmsis-dap"],
        },
        "debug": {
            "openocd_target": mcu,
            "default_tools": ["cmsis-dap"],
            "tools": {},
        },
        "frameworks": ["sifli-sdk"],
        "name": "%s (SiFli %s)" % (board, chip or mcu.upper()),
        "vendor": "SiFli",
        "url": "https://docs.sifli.com/projects/sdk/latest/%s/index.html"
               % (mcu + "x" if not mcu.endswith("x") else mcu),
        # provenance of the derived numbers, handy when regenerating
        "sifli": {"sdk_board": board, "sdk_core": core,
                  "flash_size_source": source},
    }
    if info.get("f_cpu"):
        data["build"]["f_cpu"] = info["f_cpu"]
    if flash_mb:
        data["build"]["flash_size"] = flash_mb
    if sram_kb:
        data["build"]["ram_size"] = sram_kb
        data["upload"]["maximum_ram_size"] = sram_kb * 1024
    if main_size:
        data["upload"]["maximum_size"] = main_size
        data["upload"]["maximum_flash_size"] = main_size
    if rt.get("JLINK_DEVICE"):
        data["debug"]["jlink_device"] = rt["JLINK_DEVICE"]

    return data, source


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdk", default=os.environ.get(
        "SIFLI_SDK", "/Users/mac/work/platform_sifi/SiFli-SDK"))
    ap.add_argument("--out", default=os.path.normpath(
        os.path.join(here, "..", "boards")))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    boards_root = os.path.join(args.sdk, "customer", "boards")
    if not os.path.isdir(boards_root):
        sys.exit("SiFli SDK not found: %s" % args.sdk)
    if not args.dry_run and not os.path.isdir(args.out):
        sys.exit("boards dir not found: %s" % args.out)

    written = skipped = 0
    unknown = []
    for board in sorted(os.listdir(boards_root)):
        if board in EXTRA_SKIP:
            continue
        result = gen_board(args.sdk, board)
        if result is None:
            skipped += 1
            continue
        data, source = result
        if source == "unknown":
            unknown.append(board)
        if args.dry_run:
            print("[dry-run] %-38s -> %-38s flash=%s (%s)"
                  % (board, data["build"]["board"],
                     data["build"].get("flash_size", "-"), source))
        else:
            path = os.path.join(args.out, board + ".json")
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
        written += 1

    print("boards written: %d (skipped %d non-target dirs)" % (written, skipped))
    if unknown:
        print("flash_size could not be derived (field omitted) for %d boards: %s"
              % (len(unknown), ", ".join(unknown)))


if __name__ == "__main__":
    main()
