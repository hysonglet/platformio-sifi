# Copyright 2025 SiFli community
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

from SCons.Script import (
    AlwaysBuild,
    COMMAND_LINE_TARGETS,
    Default,
    DefaultEnvironment,
)

from platformio.util import get_serial_ports


#
# Helpers
#


def _find_first(basedir, patterns):
    """Recursively return the first file under *basedir* matching any pattern."""
    if not os.path.isdir(basedir):
        return None
    for root, _, files in os.walk(basedir):
        for f in files:
            for p in patterns:
                if fnmatch.fnmatch(f, p):
                    return os.path.join(root, f)
    return None


def _resolve_sdk(env):
    # 1. project option (platformio.ini: `custom_sifli_sdk_dir = ...`).
    #    PlatformIO only accepts custom options in [env:*] sections when they
    #    start with `custom_` or `board_` (see platformio/project/config.py,
    #    CUSTOM_OPTION_PREFIXES); anything else triggers an "Ignore unknown
    #    configuration option" warning. The unprefixed name is still honoured for
    #    backwards compatibility.
    sdk = env.GetProjectOption("custom_sifli_sdk_dir", "")
    if not sdk:
        sdk = env.GetProjectOption("sifli_sdk_dir", "")
    # 2. environment variable
    if not sdk:
        sdk = os.environ.get("SIFLI_SDK", "")
    # 3. PlatformIO auto-installed `framework-sifli-sdk` package directory.
    #    The SDK may sit at the package root, or inside a `sifli-sdk`
    #    submodule (our recommended private wrapper layout).
    if not sdk:
        pkg = env.PioPlatform().get_package_dir("framework-sifli-sdk") or ""
        if pkg:
            if os.path.isfile(os.path.join(pkg, "export.sh")):
                sdk = pkg
            elif os.path.isfile(os.path.join(pkg, "sifli-sdk", "export.sh")):
                sdk = os.path.join(pkg, "sifli-sdk")
    if not sdk:
        sys.stderr.write(
            "Error: SiFli SDK directory is not set. Please set "
            "`sifli_sdk_dir` in the [env] section of platformio.ini "
            "or export the SIFLI_SDK environment variable.\n"
        )
        env.Exit(1)
    sdk = os.path.realpath(os.path.expanduser(sdk))
    if not os.path.isdir(sdk):
        sys.stderr.write("Error: SiFli SDK directory '%s' does not exist.\n" % sdk)
        env.Exit(1)
    return sdk


def _resolve_board(env):
    board = env.BoardConfig()
    name = board.get("build.board", env.subst("$BOARD"))
    # The SiFli build defaults to the HCPU core when no core suffix is given.
    if not name.endswith(("_hcpu", "_lcpu", "_acpu")):
        name += "_hcpu"
    return name


def _sdk_shell(env, scons_cmd):
    """Return a bash command that activates the SDK environment and runs *scons_cmd*.

    The SiFli SDK needs a one-time Python virtualenv setup
    (`tools/sifli_sdk_tools.py install-python-env`). When the SDK is auto-installed
    by PlatformIO from a git package this step has never run, so we create the venv
    here. The check+setup use the *same* `python3` that `export.sh` will use, so the
    resulting venv version stays in sync. The setup is guarded by an existence check
    because `install-python-env` would otherwise re-run `pip install --upgrade` on
    every build.
    """
    sdk_dir = env["SIFLI_SDK_DIR"]
    toolchain_dir = env.get("SIFLI_TOOLCHAIN_DIR") or ""
    # Chip family used to limit which SDK tools get downloaded (e.g. sf32lb52).
    target = env["SIFLI_BOARD"].split("-")[0]
    lines = [
        "set -e",
        'export SIFLI_SDK_PATH="%s"' % sdk_dir,
        # install-python-env refuses to run from inside a virtualenv (e.g. PIO's
        # own penv), so bootstrap with the underlying system Python when needed.
        'PYTHON_BIN=$(python3 -c \'import sys,os; print(os.path.join(sys.base_prefix, "bin", "python3"))\')',
        'VENV=$("$PYTHON_BIN" "%s/tools/sifli_sdk_tools.py" get-install-python-env 2>/dev/null || true)'
        % sdk_dir,
        'if [ "$VENV" = "Error: Python environment not found" ] || [ ! -x "$VENV/bin/python3" ]; then',
        '  "$PYTHON_BIN" "%s/tools/sifli_sdk_tools.py" install-python-env' % sdk_dir,
        '  VENV=$("$PYTHON_BIN" "%s/tools/sifli_sdk_tools.py" get-install-python-env 2>/dev/null || true)'
        % sdk_dir,
        "fi",
        'export PATH="$VENV/bin:$PATH"',
        # Install required SDK tools (sftool, gcc for this target) once. The SDK's
        # `export` step fails unless these are registered, and they are not present
        # in a freshly auto-installed git package.
        'MARKER="%s/.sifli_tools_installed"' % sdk_dir,
        'if [ ! -f "$MARKER" ]; then',
        '  "$VENV/bin/python" "%s/tools/sifli_sdk_tools.py" install --targets %s' % (sdk_dir, target),
        '  touch "$MARKER"',
        "fi",
    ]
    if toolchain_dir:
        lines.append('export PATH="%s/bin:$PATH"' % toolchain_dir)
    lines.append('export PYTHONPATH="%s/tools/build:${PYTHONPATH}"' % sdk_dir)
    # Pass PlatformIO's `build_flags` (e.g. `-DFOO=1`) to the SDK build. This
    # platform drives the SDK's own scons, which knows nothing about PlatformIO
    # options, so the flags travel as an environment variable and the project's
    # SConstruct applies them to the build environment (see the README).
    build_flags = env.GetProjectOption("build_flags", []) or env.get("BUILD_FLAGS") or []
    if isinstance(build_flags, str):
        # `build_flags` is a multiple option; multi-line values arrive as one string
        build_flags = [f for f in build_flags.splitlines() if f.strip()]
    if build_flags:
        joined = " ".join(str(flag).strip() for flag in build_flags if str(flag).strip())
        lines.append("export SIFLI_BUILD_FLAGS=%s" % shlex.quote(joined))

    # Installed `lib_deps` live in .pio/libdeps/<env>; PlatformIO's Library
    # Dependency Finder never runs for this platform, so the libraries would be
    # fetched but never compiled. Export their directories so the project's
    # SConscript can add the sources and include paths (see the README).
    libdeps_dir = os.path.join(
        env.subst("$PROJECT_DIR"), ".pio", "libdeps", env.subst("$PIOENV")
    )
    if os.path.isdir(libdeps_dir):
        dirs = []
        for name in sorted(os.listdir(libdeps_dir)):
            path = os.path.join(libdeps_dir, name)
            if os.path.isdir(path):
                dirs.append(path)
            elif name.endswith(".pio-link"):
                target = _piolib_link_target(path)
                if target:
                    dirs.append(target)
        if dirs:
            lines.append(
                "export SIFLI_LIBDEPS_DIRS=%s" % shlex.quote(" ".join(dirs))
            )
    lines.append('. "%s/export.sh"' % sdk_dir)
    lines.append(scons_cmd)
    return "\n".join(lines)


def _run_in_sdk_env(env, command, cwd):
    """Execute *command* inside a bash subshell with the SDK environment sourced."""
    rc = subprocess.run(["bash", "-c", command], cwd=cwd)
    if rc.returncode != 0:
        env.Exit(rc.returncode)


#
# Build / upload / clean actions
#


def _note_console_baud(env, build_dir):
    """Point out a `monitor_speed` that does not match the firmware's console baud.

    The console baud rate is a project setting (`CONFIG_RT_SERIAL_DEFAULT_BAUDRATE`;
    the SDK's RT-Thread default is 1000000), while `pio run -t monitor` decodes at
    PlatformIO's `monitor_speed` option (default 9600). A mismatch only produces
    garbled output, so mention it once after the build - that is also why the
    board manifests carry no `monitor.speed`: PlatformIO 6.x ignores it.
    """
    rtconfig = os.path.join(build_dir, "rtconfig.h")
    if not os.path.isfile(rtconfig):
        return
    with open(rtconfig, "r", errors="ignore") as f:
        text = f.read()
    match = re.search(r"#define\s+RT_SERIAL_DEFAULT_BAUDRATE\s+(\d+)", text)
    if not match:
        return

    baud = match.group(1)
    # Read the raw value: `monitor_speed` is a click.INT option, so asking
    # GetProjectOption for a string default would raise. 9600 is PlatformIO's own
    # default, used when the option is absent.
    try:
        raw = env.GetProjectConfig().getraw(
            "env:" + env.subst("$PIOENV"), "monitor_speed", "9600"
        )
    except Exception:
        raw = "9600"
    monitor = str(raw).strip()
    if monitor == baud:
        return

    sys.stdout.write(
        "[siFli] console baud is %s but `monitor_speed` is %s; add "
        "`monitor_speed = %s`\n"
        "        to platformio.ini so `pio run -t monitor` decodes the log correctly.\n"
        % (baud, monitor, baud)
    )


def _build_action(target, source, env):
    sdk_dir = env["SIFLI_SDK_DIR"]
    board_name = env["SIFLI_BOARD"]
    project_dir = env["PROJECT_DIR"]
    jobs = env.GetOption("num_jobs")

    command = _sdk_shell(env, "scons --board=%s -j%d" % (board_name, jobs))
    _run_in_sdk_env(env, command, project_dir)

    build_dir = os.path.join(project_dir, "build_%s" % board_name)
    elf = _find_first(build_dir, ["*.elf"])
    if not elf:
        sys.stderr.write(
            "Error: `scons` finished but no .elf was produced in %s.\n" % build_dir
        )
        env.Exit(1)

    firm_elf = os.path.join(build_dir, "firmware.elf")
    shutil.copy(elf, firm_elf)

    binf = _find_first(build_dir, ["ER_IROM1.bin", "main.bin"])
    firm_bin = os.path.join(build_dir, "firmware.bin")
    if binf:
        shutil.copy(binf, firm_bin)
    else:
        objcopy = env.get("OBJCOPY") or "arm-none-eabi-objcopy"
        subprocess.run([objcopy, "-Obinary", firm_elf, firm_bin], check=False)

    try:
        _note_console_baud(env, build_dir)
    except Exception as exc:            # a hint must never break the build
        sys.stdout.write("[siFli] console baud check skipped: %s\n" % exc)


# Strings that hint at a USB serial adapter usable for the UART download.
_USB_SERIAL_HINTS = (
    "usbserial", "usb serial", "usb-serial", "cp210", "ch34", "ftdi",
    "silicon labs", "sifli", "jtag", "uart",
)


def _piolib_link_target(path):
    """Resolve the source directory behind a `.pio-link` entry.

    Libraries installed from a local path (`symlink://` / `file://`) are a JSON
    metadata file instead of a directory, e.g.
    `{"spec": {"uri": "symlink:///abs/path/to/lib"}}`.
    """
    try:
        with open(path, "r") as f:
            uri = json.load(f).get("spec", {}).get("uri") or ""
    except Exception:
        return None
    for scheme in ("symlink://", "file://"):
        if uri.startswith(scheme):
            target = uri[len(scheme):]
            return target if os.path.isdir(target) else None
    return None


def _looks_like_usb_serial(entry):
    blob = " ".join(str(entry.get(k, "")) for k in ("port", "description", "hwid")).lower()
    return any(hint in blob for hint in _USB_SERIAL_HINTS)


def _ports_listing():
    ports = get_serial_ports() or []
    return "\n".join(
        "    %-24s %s" % (p.get("port", "?"), p.get("description", "") or "-")
        for p in ports
    ) or "    <none detected>"


def _detect_upload_port(env):
    """Return the serial port to download over.

    Priority: `upload_port` from platformio.ini, then a USB serial adapter
    reported by the OS (it must be unambiguous), otherwise fail with the list of
    detected ports instead of letting the SDK script prompt interactively.
    """
    configured = env.get("UPLOAD_PORT") or env.subst("$UPLOAD_PORT")
    if configured:
        # `upload_port = a, b` gives a list; the first entry is used.
        if isinstance(configured, (list, tuple)):
            configured = configured[0] if configured else ""
        configured = str(configured).strip()
        if configured:
            # A POSIX device path can be checked up front; failing here is much
            # friendlier than sftool's "failed to open port" afterwards.
            if configured.startswith("/") and not os.path.exists(configured):
                sys.stderr.write(
                    "Error: upload_port '%s' does not exist.\n"
                    "  Detected serial ports:\n%s\n"
                    "  Fix `upload_port` in platformio.ini, or remove it to let "
                    "PlatformIO use the detected adapter.\n"
                    % (configured, _ports_listing())
                )
                env.Exit(1)
            return configured

    ports = get_serial_ports() or []
    candidates = [p for p in ports if _looks_like_usb_serial(p)]
    if len(candidates) == 1:
        return candidates[0]["port"]
    if len(ports) == 1:
        return ports[0]["port"]

    sys.stderr.write(
        "Error: cannot determine which serial port to use for the UART download.\n"
        "  Detected serial ports:\n%s\n"
        "  Set it explicitly in platformio.ini, e.g.:\n"
        "      upload_port = /dev/cu.usbserial-1130        (macOS/Linux)\n"
        "      upload_port = COM3                          (Windows)\n" % _ports_listing()
    )
    env.Exit(1)


def _sftool_config_command(build_dir, param_json, port):
    """Build a `sftool -f <config>` command from the generated param file.

    Fallback for when the SDK's `uart_download.sh` is missing: sftool's config
    file has to carry the port itself (0.1.16 rejects `-f` without it), so a copy
    of `sftool_param.json` with the port added is written next to it.
    """
    import json

    with open(param_json, "r") as f:
        config = json.load(f)
    config["port"] = port
    config.setdefault("chip", config.get("chip"))
    out = os.path.join(build_dir, "sftool_upload.json")
    with open(out, "w") as f:
        json.dump(config, f, indent=2)
    # The file paths inside the config are relative to the build directory.
    return 'sftool -f "%s"' % os.path.basename(out)


def _upload_action(target, source, env):
    port = _detect_upload_port(env)

    board_name = env["SIFLI_BOARD"]
    project_dir = env["PROJECT_DIR"]
    build_dir = os.path.join(project_dir, "build_%s" % board_name)

    # Regenerated by the SDK on every build (`GenDownloadScript` in the SDK's
    # tools/build/building.py, template in tools/build/resource.py).
    sh = os.path.join(build_dir, "uart_download.sh")
    param_json = os.path.join(build_dir, "sftool_param.json")

    if os.path.isfile(sh):
        command = 'bash "%s" -p "%s"' % (sh, port)
    elif os.path.isfile(param_json):
        command = _sftool_config_command(build_dir, param_json, port)
    else:
        sys.stderr.write(
            "Error: no download script found in %s\n"
            "       (expected uart_download.sh or sftool_param.json).\n"
            "       Run a build first, e.g. `pio run`.\n" % build_dir
        )
        env.Exit(1)

    sys.stdout.write("Downloading %s over %s\n" % (board_name, port))
    _run_in_sdk_env(env, _sdk_shell(env, command), build_dir)


def _clean_action(target, source, env):
    sdk_dir = env["SIFLI_SDK_DIR"]
    board_name = env["SIFLI_BOARD"]
    project_dir = env["PROJECT_DIR"]

    command = _sdk_shell(env, "scons --board=%s -c" % board_name)
    _run_in_sdk_env(env, command, project_dir)


#
# Main
#

env = DefaultEnvironment()
platform = env.PioPlatform()

sdk_dir = _resolve_sdk(env)
board_name = _resolve_board(env)
toolchain_dir = platform.get_package_dir("toolchain-gccarmnoneeabi") or ""

project_dir = env.subst("$PROJECT_DIR")

env.Replace(
    SIFLI_SDK_DIR=sdk_dir,
    SIFLI_BOARD=board_name,
    PROJECT_DIR=project_dir,
    SIFLI_TOOLCHAIN_DIR=toolchain_dir,
)

build_dir = os.path.join(project_dir, "build_%s" % board_name)
firm_elf = os.path.join(build_dir, "firmware.elf")
firm_bin = os.path.join(build_dir, "firmware.bin")

# Build via the SDK's SCons (always run; outputs are copied to stable names).
build_nodes = env.Command([firm_elf, firm_bin], [], _build_action)
AlwaysBuild(build_nodes)
firm_elf_node = build_nodes[0]
firm_bin_node = build_nodes[1]

target_size = env.AddPlatformTarget(
    "size",
    firm_elf_node,
    env.VerboseAction("$SIZEPRINTCMD", "Calculating size $SOURCE"),
    "Program Size",
    "Calculate program size",
)

target_buildprog = env.Alias("buildprog", firm_bin_node)
env.AddPlatformTarget(
    "upload",
    firm_bin_node,
    [env.VerboseAction(_upload_action, "Uploading $SOURCE")],
    "Upload",
)
# `pio run -t download` is an alias for upload, using the SDK's wording.
env.AddPlatformTarget(
    "download",
    firm_bin_node,
    [env.VerboseAction(_upload_action, "Downloading $SOURCE")],
    "Download",
)
env.AddPlatformTarget(
    "sifli-clean",
    env.Command("sifli_clean", [], _clean_action),
    None,
    "Clean (scons -c)",
)

# PIO's core expects a `checkprogsize` target; it is normally created by
# `env.BuildProgram()`, which we bypass by driving the SDK's own SCons build.
env.AlwaysBuild(
    env.Alias(
        "checkprogsize",
        firm_elf_node,
        env.VerboseAction(env.CheckUploadSize, "Checking size $SOURCE"),
    )
)

Default([target_buildprog, target_size])
