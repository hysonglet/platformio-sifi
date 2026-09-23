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
import os
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
    # 1. project option (platformio.ini: `sifli_sdk_dir = ...`)
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


def _find_sdk_venv_bin():
    """Locate the SiFli SDK Python virtual environment `bin` directory.

    The SDK's `export.sh` selects a Python interpreter based on what is first on
    PATH. PlatformIO runs SCons under its own bundled Python, which would make
    `export.sh` look for a non-existent virtualenv. Prepending the SDK venv `bin`
    to PATH forces `export.sh` to use the correct interpreter (and its `scons`).
    """
    tools_path = os.environ.get("SIFLI_SDK_TOOLS_PATH") or os.path.expanduser(
        "~/.sifli"
    )
    base = os.path.join(tools_path, "python_env")
    if not os.path.isdir(base):
        return ""
    for name in sorted(os.listdir(base)):
        bin_dir = os.path.join(base, name, "bin")
        if os.path.isfile(os.path.join(bin_dir, "python3")) and os.path.isfile(
            os.path.join(bin_dir, "scons")
        ):
            return bin_dir
    return ""


def _export_prefix(sdk_dir, toolchain_dir, venv_bin):
    """Shell snippet that activates the SDK environment before running scons."""
    parts = []
    if venv_bin:
        parts.append('PATH="%s:$PATH"' % venv_bin)
    if toolchain_dir:
        parts.append('PATH="%s/bin:$PATH"' % toolchain_dir)
    # Make the SDK's `building` module importable for SConstruct/SConcript.
    parts.append('PYTHONPATH="%s/tools/build:${PYTHONPATH}"' % sdk_dir)
    parts.append('. "%s/export.sh"' % sdk_dir)
    return " ".join(parts)


def _run_in_sdk_env(env, command, cwd):
    """Execute *command* inside a bash subshell with the SDK environment sourced."""
    rc = subprocess.run(["bash", "-c", command], cwd=cwd)
    if rc.returncode != 0:
        env.Exit(rc.returncode)


#
# Build / upload / clean actions
#


def _build_action(target, source, env):
    sdk_dir = env["SIFLI_SDK_DIR"]
    board_name = env["SIFLI_BOARD"]
    project_dir = env["PROJECT_DIR"]
    toolchain_dir = env.get("SIFLI_TOOLCHAIN_DIR")
    venv_bin = _find_sdk_venv_bin()
    jobs = env.GetOption("num_jobs")

    prefix = _export_prefix(sdk_dir, toolchain_dir, venv_bin)
    command = '%s && scons --board=%s -j%d' % (prefix, board_name, jobs)
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


def _upload_action(target, source, env):
    env.AutodetectUploadPort()
    port = env.get("UPLOAD_PORT") or env.subst("$UPLOAD_PORT")

    sdk_dir = env["SIFLI_SDK_DIR"]
    board_name = env["SIFLI_BOARD"]
    project_dir = env["PROJECT_DIR"]
    toolchain_dir = env.get("SIFLI_TOOLCHAIN_DIR")

    build_dir = os.path.join(project_dir, "build_%s" % board_name)
    sh = os.path.join(build_dir, "uart_download.sh")
    if not os.path.exists(sh):
        sys.stderr.write(
            "Error: %s not found. Run a build first so the download "
            "script is generated.\n" % sh
        )
        env.Exit(1)

    prefix = _export_prefix(sdk_dir, toolchain_dir, _find_sdk_venv_bin())
    command = '%s && bash "%s" -p "%s"' % (prefix, sh, port or "")
    _run_in_sdk_env(env, command, build_dir)


def _clean_action(target, source, env):
    sdk_dir = env["SIFLI_SDK_DIR"]
    board_name = env["SIFLI_BOARD"]
    project_dir = env["PROJECT_DIR"]
    toolchain_dir = env.get("SIFLI_TOOLCHAIN_DIR")

    prefix = _export_prefix(sdk_dir, toolchain_dir, _find_sdk_venv_bin())
    command = "%s && scons --board=%s -c" % (prefix, board_name)
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
