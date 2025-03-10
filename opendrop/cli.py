"""
OpenDrop: an open source AirDrop implementation
Copyright (C) 2018  Milan Stute
Copyright (C) 2018  Alexander Heinrich

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import argparse
from http import client
import json
import logging
import os
import sys
import threading
import time
from typing import Optional, Dict

from .client import AirDropBrowser, AirDropClient
from .config import AirDropConfig, AirDropReceiverFlags
from .server import AirDropServer

logger = logging.getLogger(__name__)


def main():
    AirDropCli(sys.argv[1:])


class AirDropCli:
    def __init__(self, args):
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "action",
            choices=[
                "receive",
                "find",
                "send",
                "discover",
                "ask",
                "upload",
                "askupload",
            ],
        )
        parser.add_argument("-f", "--file", help="File to be sent")
        parser.add_argument(
            "-r",
            "--receiver",
            help="Peer to send file to (can be index, ID, or hostname)",
        )
        parser.add_argument(
            "-e", "--email", nargs="*", help="User's email addresses (currently unused)"
        )
        parser.add_argument(
            "-p", "--phone", nargs="*", help="User's phone numbers (currently unused)"
        )
        parser.add_argument(
            "-n", "--name", help="Computer name (displayed in sharing pane)"
        )
        parser.add_argument(
            "-m", "--model", help="Computer model (displayed in sharing pane)"
        )
        parser.add_argument(
            "-d", "--debug", help="Enable debug mode", action="store_true"
        )
        parser.add_argument(
            "-i", "--interface", help="Which AWDL interface to use", default="awdl0"
        )

        parser.add_argument(
            "-I",
            "--no-interface",
            action="store_true",
            help="Do not use awdl interface for connection (allows use of global IPv6 addresses)",
        )

        parser.add_argument(
            "-A", "--address", help="Address to send raw messages", required=False
        )
        parser.add_argument(
            "-P", "--port", help="Port to send raw message to", default=8770
        )
        parser.add_argument(
            "-R", "--rawcpio", help="Raw cpio file to upload", required=False
        )
        parser.add_argument(
            "-J",
            "--payload",
            help="JSON data containing payload to send with ask/discover",
            required=False,
        )
        parser.add_argument(
            "-B",
            "--binpayload",
            help="Raw payload send with ask/discover",
            required=False,
        )
        args = parser.parse_args(args)

        if args.debug:
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            )
        else:
            logging.basicConfig(level=logging.INFO, format="%(message)s")

        # TODO put emails and phone in canonical form (lower case, no '+' sign, etc.)

        ifname = args.interface
        if args.no_interface:
            logger.info("Clearing interface")
            ifname = None

        self.config = AirDropConfig(
            email=args.email,
            phone=args.phone,
            computer_name=args.name,
            computer_model=args.model,
            debug=args.debug,
            interface=ifname,
        )
        self.server = None
        self.client = None
        self.browser = None
        self.sending_started = False
        self.discover = []
        self.lock = threading.Lock()
        # custom payloads for ask/discover
        self.custom_payload: Optional[Dict] = None
        self.raw_payload: Optional[bytes] = None

        try:
            # Read custom payloads
            if args.payload is not None:
                if not os.path.isfile(args.payload):
                    parser.error(f"custom payload file {args.payload} not found")
                self.custom_payload = _read_json_payload(args.payload)
            if args.binpayload is not None:
                if not os.path.isfile(args.binpayload):
                    parser.error(f"custom payload file {args.binpayload} not found")
                self.raw_payload = _read_binary_payload(args.binpayload)

            if args.action == "receive":
                self.receive()
            elif args.action == "find":
                self.find()
            elif args.action == "discover":
                if args.address is None:
                    parser.error("Need -A, --address when using raw discover")
                self._address = args.address
                self._port = args.port
                self.cmd_discover()
            elif (
                args.action == "ask"
                or args.action == "upload"
                or args.action == "askupload"
            ):
                if args.address is None:
                    parser.error(
                        "Need -A, --address when using raw ask/upload/askupload"
                    )
                self._address = args.address
                self._port = args.port
                if args.file is not None:
                    if not os.path.isfile(args.file):
                        parser.error("File in -f,--file not found")
                    self.file = args.file
                else:
                    self.file = None

                if args.rawcpio is not None:
                    if not os.path.isfile(args.rawcpio):
                        parser.error("File in -R,--rawcipio not found")
                    self.rawcpio = args.rawcpio
                else:
                    self.rawcpio = None

                if args.action == "ask":
                    self.cmd_ask()
                elif args.action == "upload":
                    self.cmd_upload()
                else:  # "askupload"
                    self.cmd_askupload()

            elif args.action == "send":
                if args.file is None:
                    parser.error("Need -f,--file when using send")
                if not os.path.isfile(args.file):
                    parser.error("File in -f,--file not found")
                self.file = args.file
                if args.receiver is None:
                    parser.error("Need -r,--receiver when using send")
                self.receiver = args.receiver
                self.send()
            else:
                parser.error(f"unsupported action {args.action}")
        except KeyboardInterrupt:
            if self.browser is not None:
                self.browser.stop()
            if self.server is not None:
                self.server.stop()

    def find(self):
        logger.info("Looking for receivers. Press Ctrl+C to stop ...")
        self.browser = AirDropBrowser(self.config)
        self.browser.start(callback_add=self._found_receiver)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            self.browser.stop()
            logger.debug(f"Save discovery results to {self.config.discovery_report}")
            with open(self.config.discovery_report, "w") as f:
                json.dump(self.discover, f)

    def _found_receiver(self, info):
        thread = threading.Thread(target=self._send_discover, args=(info,))
        thread.start()

    def _send_discover(self, info):
        try:
            address = info.parsed_addresses()[0]  # there should only be one address
        except IndexError:
            logger.warning(f"Ignoring receiver with missing address {info}")
            return
        identifier = info.name.split(".")[0]
        hostname = info.server
        port = int(info.port)
        logger.debug(
            f"AirDrop service found: {hostname}, {address}:{port}, ID {identifier}"
        )
        client = AirDropClient(self.config, (address, int(port)))
        try:
            flags = int(info.properties[b"flags"])
        except KeyError:
            # TODO in some cases, `flags` are not set in service info; for now we'll try anyway
            flags = AirDropReceiverFlags.SUPPORTS_DISCOVER_MAYBE

        if flags & AirDropReceiverFlags.SUPPORTS_DISCOVER_MAYBE:
            try:
                receiver_name = client.send_discover(
                    payload=self.custom_payload, binpayload=self.raw_payload
                )
            except TimeoutError:
                receiver_name = None
        else:
            receiver_name = None
        discoverable = receiver_name is not None

        index = len(self.discover)
        node_info = {
            "name": receiver_name,
            "address": address,
            "port": port,
            "id": identifier,
            "flags": flags,
            "discoverable": discoverable,
        }
        self.lock.acquire()
        self.discover.append(node_info)
        if discoverable:
            logger.info(f"Found  index {index}  ID {identifier}  name {receiver_name}")
        else:
            logger.debug(f"Receiver ID {identifier} is not discoverable")
        self.lock.release()

    def _send_discover_to(self, address: str, port: int):
        logger.debug(f"Sending Discover to [{address}]:[{port}] ")
        client = AirDropClient(self.config, (address, port))
        client.send_discover(payload=self.custom_payload, binpayload=self.raw_payload)

    def _send_ask_to(self, address: str, port: int):
        logger.debug(f"Sending Ask to [{address}]:{port}")
        client = AirDropClient(self.config, (address, port))
        client.send_ask(
            self.file, payload=self.custom_payload, binpayload=self.raw_payload
        )

    def _send_upload_to(self, address: str, port: int):
        logger.debug(f"Sending Upload to [{address}]:{port}")
        if self.file is None and self.rawcpio is None:
            logger.error("Need either -f,--file or -R,--rawcpio")
            return
        client = AirDropClient(self.config, (address, port))
        client.send_upload(self.file, self.rawcpio)

    def _send_ask_and_upload(self, address: str, port: int):
        logger.debug(f"Sending Ask-Upload to [{address}]:{port}")
        if self.file is None and self.rawcpio is None:
            logger.error("Need either -f,--file or -R,--rawcpio")
            return
        client = AirDropClient(self.config, (address, port))
        logger.debug("..Ask")
        client.send_ask(
            self.file, payload=self.custom_payload, binpayload=self.raw_payload
        )
        logger.debug("..Upload")
        client.send_upload(self.file, self.rawcpio)

    def cmd_discover(self):
        self._send_discover_to(self._address, self._port)

    def cmd_ask(self):
        self._send_ask_to(self._address, self._port)

    def cmd_upload(self):
        self._send_upload_to(self._address, self._port)

    def cmd_askupload(self):
        self._send_ask_and_upload(self._address, self._port)

    def receive(self):
        self.server = AirDropServer(self.config)
        self.server.start_service()
        self.server.start_server()

    def send(self):
        info = self._get_receiver_info()
        if info is None:
            return
        self.client = AirDropClient(self.config, (info["address"], info["port"]))
        logger.info("Asking receiver to accept ...")
        if not self.client.send_ask(
            self.file, payload=self.custom_payload, binpayload=self.raw_payload
        ):
            logger.warning("Receiver declined")
            return
        logger.info("Receiver accepted")
        logger.info("Uploading file ...")
        if not self.client.send_upload(self.file, None):
            logger.warning("Uploading has failed")
            return
        logger.info("Uploading has been successful")

    def _get_receiver_info(self):
        if not os.path.exists(self.config.discovery_report):
            logger.error("No discovery report exists, please run 'opendrop find' first")
            return None
        age = time.time() - os.path.getmtime(self.config.discovery_report)
        if age > 60:  # warn if report is older than a minute
            logger.warning(
                f"Old discovery report ({age:.1f} seconds), consider running 'opendrop find' again"
            )
        with open(self.config.discovery_report, "r") as f:
            infos = json.load(f)

        # (1) try 'index'
        try:
            self.receiver = int(self.receiver)
            return infos[self.receiver]
        except ValueError:
            pass
        except IndexError:
            pass
        # (2) try 'id'
        if len(self.receiver) == 12:
            for info in infos:
                if info["id"] == self.receiver:
                    return info
        # (3) try hostname
        for info in infos:
            if info["name"] == self.receiver:
                return info
        # (fail)
        logger.error(
            "Receiver does not exist (check -r,--receiver format or try 'opendrop find' again"
        )
        return None


def _read_json_payload(fname: str) -> Optional[Dict]:
    try:
        with open(fname, "r") as f:
            payload = json.load(f)
        return payload
    except Exception as ex:
        logger.warning(f"Unable to read JSON payload: {ex}")
        return None


def _read_binary_payload(fname: str) -> Optional[bytes]:
    try:
        with open(fname, "rb") as f:
            payload = f.read()
        return payload
    except Exception as ex:
        logger.warning(f"Unable to read binary payload: {ex}")
        return None
