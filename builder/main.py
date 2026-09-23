# Copyright 2014-present PlatformIO <contact@platformio.org>
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

from os.path import join

from SCons.Script import DefaultEnvironment

env = DefaultEnvironment()
platform = env.PioPlatform()

toolchain_dir = platform.get_package_dir("toolchain-gccarmnoneeabi") or ""

env.Replace(
    AR="arm-none-eabi-gcc-ar",
    AS="arm-none-eabi-as",
    CC="arm-none-eabi-gcc",
    CXX="arm-none-eabi-g++",
    GDB="arm-none-eabi-gdb",
    OBJCOPY="arm-none-eabi-objcopy",
    OBJDUMP="arm-none-eabi-objdump",
    RANLIB="arm-none-eabi-gcc-ranlib",
    SIZETOOL=join(toolchain_dir, "bin", "arm-none-eabi-size")
    if toolchain_dir
    else "arm-none-eabi-size",
    ARFLAGS=["rc"],
    SIZEPROGREGEXP=r"^(?:\.text|\.data|\.bss|\.rodata|\.ARM.exidx|\.ARM.extab)\s+([0-9]+).*",
    SIZEDATAREGEXP=r"^(?:\.data|\.bss|\.noinit)\s+([0-9]+).*",
    SIZECHECKCMD="$SIZETOOL -A -d $SOURCES",
    SIZEPRINTCMD="$SIZETOOL -B -d $SOURCES",
    PROGSUFFIX=".elf",
)

# Allow user to override via pre:script
if env.get("PROGNAME", "program") == "program":
    env.Replace(PROGNAME="firmware")

# Dispatch to the SiFli SDK framework builder, which wraps the SDK's SCons build.
env.SConscript("frameworks/sifli.py")
