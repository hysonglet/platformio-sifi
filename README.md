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
pio run -t upload       # flash via uart_download.sh (needs a serial port)
```

> The default `pio run -t clean` only removes PlatformIO's `.pio/` folder;
> use `pio run -t sifli-clean` to clean the SDK `build_*` directory.

### Upload

`upload` runs the SDK-generated `uart_download.sh` from the build directory,
passing the detected (or configured) serial port:

```ini
upload_port = /dev/cu.usbserial-XXXX   ; macOS/Linux
; upload_port = COM3                   ; Windows
```

## Adding a board

Available board names live in the SDK under `customer/boards/` (and
`bf0_ap/boards/`). To add a new board, copy `boards/sf32lb52-lcd_n16r8.json`,
set `build.board` to the board directory name (the `_hcpu` core suffix is
added automatically), and adjust `mcu` / `flash_size` / `ram_size` /
`maximum_size` as needed.

## Notes / limitations

* **Debug** (`cmsis-dap` / `jlink`) targets are declared but require a
  SiFli-aware OpenOCD/ J-Link setup; they are best-effort and may need extra
  configuration (`debug.openocd_target`, `debug.jlink_device`).
* The `sifli_sdk_dir` option emits a harmless "unknown configuration option"
  warning from PlatformIO; set the `SIFLI_SDK` environment variable instead if
  you want a clean log.
* This package lives entirely outside the SDK, so the SDK stays pristine.
