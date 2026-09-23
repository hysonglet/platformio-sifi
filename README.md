# SiFli Platform for PlatformIO

A community [PlatformIO](https://platformio.org/) platform that adds support for
**SiFli SF32LBxx** microcontrollers (e.g. SF32LB52x). It wraps the official
SiFli SDK build system (RT-Thread based, driven by `scons`) so that
`pio run` builds your application exactly the way the vendor tooling does,
without touching the SDK sources.

The SDK **is not modified** and no files are added to it. All PlatformIO
support files live in this directory; build artifacts are written into your
project folder (`build_<board>_hcpu/`).

## How it works

PlatformIO itself does not understand the SiFli SDK build. Instead, this
platform's builder (`builder/frameworks/sifli.py`) sources the SDK's
`export.sh` (to set up the SDK's Python virtualenv, `PYTHONPATH`, `SIFLI_SDK`,
etc.) and then shells out to the SDK's own `scons --board=<board>` command.
The resulting `firmware.elf` / `firmware.bin` are produced in the SDK build
directory and reused for the `size` / `upload` targets.

## Prerequisites

1. A **SiFli SDK** checkout (this package does not include it). Follow the
   vendor "Getting Started" to install its Python environment
   (`export.sh` / `install.sh`). Tested against the SDK that provides
   `export.sh` at its root.
2. Python 3.9+ (the SDK's virtualenv is detected automatically).
3. An ARM GCC toolchain — provided automatically by PlatformIO as the
   `toolchain-gccarmnoneeabi` package.

## Installation / usage

Point `platform` at the **absolute path** of this folder in your
`platformio.ini`:

```ini
[env:sf32lb52-lcd_n16r8]
platform = /abs/path/to/sifli-platform
board    = sf32lb52-lcd_n16r8
framework = sifli-sdk

; Tell the builder where the SDK lives (either option works):
sifli_sdk_dir = /abs/path/to/SiFli-SDK
; or export SIFLI_SDK=/abs/path/to/SiFli-SDK in your shell
```

Your project **must be a valid SiFli application**. Copy the files from
`examples/hello_world/` into your project root:

```
your-project/
├── platformio.ini
├── SConstruct
├── SConscript
├── rtconfig.py
├── rtconfig_project.h
├── Kconfig
├── Kconfig.proj
├── proj.conf
└── src/
    ├── SConscript
    └── main.c
```

Then build:

```bash
pio run                 # compile + size check
pio run -t size         # print memory usage
pio run -t sifli-clean  # run `scons -c` (remove SDK build artifacts)
pio run -t upload       # flash over the serial port (see "Upload / download")
pio run -t download     # alias of upload
```

> The default `pio run -t clean` only removes PlatformIO's `.pio/` folder;
> use `pio run -t sifli-clean` to clean the SDK `build_*` directory.

### Upload / download

Every build regenerates the SDK's UART download script and its parameter file in
`build_<board>_hcpu/`:

* `uart_download.sh` / `uart_download.bat` (`GenDownloadScript` in the SDK's
  `tools/build/building.py`, templates in `tools/build/resource.py`)
* `sftool_param.json` — the same write list in machine-readable form

The platform reuses those artifacts, so the chip (`-c`, derived from the board's
`JLINK_DEVICE`), the memory type (`-m`) and the addresses always match the build:

```bash
pio run -t upload     # build if needed, then flash over the serial port
pio run -t download   # same action, SDK wording
```

The serial port is resolved in this order:

1. `upload_port` from `platformio.ini` — a POSIX device path is verified first, so
   a stale value is reported instead of failing inside `sftool`:

   ```ini
   upload_port = /dev/cu.usbserial-XXXX   ; macOS/Linux
   ; upload_port = COM3                   ; Windows
   ```

   (`upload_port = a, b` uses the first entry.)
2. otherwise the USB serial adapter reported by the OS, if it is unambiguous;
3. otherwise the upload aborts and prints the list of detected ports.

If `uart_download.sh` is missing (for example the build directory was cleaned
without rebuilding), the platform falls back to calling `sftool -f <config>`
directly, using a copy of `sftool_param.json` with the port injected.

## Boards

All board targets the SDK can build for are already provided:

```bash
python3 tools/gen_boards.py            # regenerate ../boards from the SDK
python3 tools/gen_boards.py --dry-run  # show what would be written
```

The generator walks `customer/boards/`, keeps every directory that has an
`hcpu`/`lcpu` sub-directory (i.e. what `scons --board=<name>_hcpu` can resolve)
and derives the fields from the SDK: `build.board` from the directory name,
`debug.jlink_device` from `<core>/rtconfig.py`, `upload.maximum_size` from
`ptab.json`, and `flash_size` from the board name / `doc/` module name /
`board.conf` (`FLASH_SIZE_OVERRIDES` fills the gaps). Directories that are not
selectable targets (`*_base` BSP bases, `common/`, `include/`, `dummy/`, `pc/`
and the flat legacy BSP dirs) are skipped.

To add a board by hand, copy one of the JSON files in `boards/` and set
`build.board` to the SDK board directory name (the `_hcpu` default core suffix is
appended automatically).

## Notes / limitations

* **Debug** (`cmsis-dap` / `jlink`) targets are declared but require a
  SiFli-aware OpenOCD/ J-Link setup; they are best-effort and may need extra
  configuration (`debug.openocd_target`, `debug.jlink_device`).
* The `sifli_sdk_dir` option emits a harmless "unknown configuration option"
  warning from PlatformIO; set the `SIFLI_SDK` environment variable instead if
  you want a clean log.
* This package lives entirely outside the SDK, so the SDK stays pristine.
