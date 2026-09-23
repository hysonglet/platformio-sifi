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

import sys

from platformio.managers.platform import PlatformBase


class SifliPlatform(PlatformBase):
    def configure_default_packages(self, variables, targets):
        board = variables.get("board")
        frameworks = variables.get("pioframework", [])

        # The SDK build needs the ARM GCC toolchain on the PATH.
        if "toolchain-gccarmnoneeabi" in self.packages:
            self.packages["toolchain-gccarmnoneeabi"]["optional"] = False

        # Make debugger/upload helpers available when requested.
        upload_protocol = variables.get("upload_protocol")
        if upload_protocol == "jlink" and "tool-jlink" in self.packages:
            self.packages["tool-jlink"]["optional"] = False
        if variables.get("debug_tool") or upload_protocol in (
            "cmsis-dap",
            "jlink",
        ):
            if "tool-openocd" in self.packages:
                self.packages["tool-openocd"]["optional"] = False

        return PlatformBase.configure_default_packages(self, variables, targets)

    def get_boards(self, id_=None):
        result = PlatformBase.get_boards(self, id_)
        if not result:
            return result
        if id_:
            return self._add_default_debug_tools(result)
        for key, value in result.items():
            result[key] = self._add_default_debug_tools(result[key])
        return result

    def _add_default_debug_tools(self, board):
        debug = board.manifest.get("debug", {})
        upload_protocols = board.manifest.get("upload", {}).get("protocols", [])
        if "tools" not in debug:
            debug["tools"] = {}

        for link in ("cmsis-dap", "jlink", "blackmagic"):
            if link not in upload_protocols or link in debug["tools"]:
                continue
            if link == "blackmagic":
                debug["tools"]["blackmagic"] = {
                    "hwids": [["0x1d50", "0x6018"]],
                    "require_debug_port": True,
                }
            elif link == "jlink":
                assert debug.get("jlink_device"), (
                    "Missed J-Link Device ID for %s" % board.id
                )
                debug["tools"][link] = {
                    "server": {
                        "package": "tool-jlink",
                        "arguments": [
                            "-singlerun",
                            "-if",
                            "SWD",
                            "-select",
                            "USB",
                            "-device",
                            debug.get("jlink_device"),
                            "-port",
                            "2331",
                        ],
                        "executable": (
                            "JLinkGDBServerCL.exe"
                            if sys.platform.startswith("win")
                            else "JLinkGDBServer"
                        ),
                    }
                }
            else:
                server_args = ["-s", "$PACKAGE_DIR/scripts"]
                if debug.get("openocd_board"):
                    server_args.extend(
                        ["-f", "board/%s.cfg" % debug.get("openocd_board")]
                    )
                else:
                    assert debug.get("openocd_target"), (
                        "Missed target configuration for %s" % board.id
                    )
                    server_args.extend(
                        [
                            "-f",
                            "interface/%s.cfg" % link,
                            "-c",
                            "transport select swd",
                            "-f",
                            "target/%s.cfg" % debug.get("openocd_target"),
                        ]
                    )
                    server_args.extend(debug.get("openocd_extra_args", []))

                debug["tools"][link] = {
                    "server": {
                        "package": "tool-openocd",
                        "executable": "bin/openocd",
                        "arguments": server_args,
                    }
                }
            debug["tools"][link]["onboard"] = link in debug.get("onboard_tools", [])
            debug["tools"][link]["default"] = link in debug.get("default_tools", [])

        board.manifest["debug"] = debug
        return board
