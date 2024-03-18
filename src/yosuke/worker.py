import asyncio
import datetime
import json
import logging
import pathlib
import socket
import sys
import typing
import uuid
from dataclasses import dataclass, field
from functools import wraps
from importlib.metadata import version

import aiofiles
import aiofiles.os


async def get_current_system_version() -> str:
    proc = await asyncio.create_subprocess_exec(
        "uname",
        "-r",
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip()


async def get_systemd_version() -> str:
    proc = await asyncio.create_subprocess_exec(
        "systemctl",
        "--version",
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    version_line = stdout.decode().splitlines()[0]
    return version_line.split()[1]


@dataclass
class Probe:
    """A single probe data point."""

    name: str
    system: str

    mediaType: str
    version: str
    content: str = ""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    observed: datetime.datetime = datetime.datetime.min
    interval: datetime.timedelta = datetime.timedelta(0)
    duration: datetime.timedelta = datetime.timedelta(0)

    status: int = -1
    stderr: str = ""

    def __enter__(self):
        if self.observed == datetime.datetime.min:
            # Allow using the context manager multiple times but keep the first
            # actual observed time.
            self.observed = datetime.datetime.utcnow()
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.duration = datetime.datetime.utcnow() - self.observed

    def to_json(self):
        return json.dumps(
            {
                # XXX
                "version": self.version,
                "content": self.content,
                "observed": self.observed.isoformat(),
                "interval": self.interval.total_seconds(),
                "duration": self.duration.total_seconds(),
                "system": self.system,
                "id": self.id,
                "status": self.status,
                "stderr": self.stderr,
            },
            indent=2,
        )

    async def from_command(self, *args, **kw):
        with self:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kw,
            )
            stdout, stderr = await proc.communicate()
            self.content = stdout.decode()
            self.stderr = stderr.decode()
            assert proc.returncode is not None
            self.status = proc.returncode

    async def from_file(self, path: pathlib.Path):
        with self:
            async with aiofiles.open(path, mode="r") as f:
                self.content = await f.read()
            self.status = 0


class UnitOfWork:

    probes: list[Probe]

    def __init__(self, workdir: "pathlib.Path"):
        self.workdir = workdir
        self.probes = []

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.probes = []

    async def commit(self):
        logging.debug("Committing ...")
        for probe in self.probes:
            try:
                await self._commit_probe(probe)
            except Exception:
                logging.exception("Error committing probe")
                continue

    async def _commit_probe(self, probe):
        logging.info(
            f"Committing probe {probe.mediaType}@{probe.version} "
            f"{probe.name} {probe.id} ..."
        )

        tmpfile = self.workdir / "outbox" / f".{probe.id}.json"
        finalfile = self.workdir / "outbox" / f"{probe.id}.json"

        async with aiofiles.open(tmpfile, "w") as f:
            data = probe.to_json()
            await f.write(data)

        await aiofiles.os.rename(tmpfile, finalfile)


class Plugin:

    name: str
    mediaType: str
    version: str
    system: str
    config: dict[str, typing.Any]

    probe_factory = Probe
    uow: UnitOfWork
    interval: datetime.timedelta

    def __init__(self, uow, system, interval):
        self.uow = uow
        self.system = system
        self.interval = interval

    async def _ainit(self):
        return True

    def new_probe(self, name=None, mediaType=None, version=None):
        if name is None:
            name = self.name
        if mediaType is None:
            mediaType = self.mediaType
        if version is None:
            version = self.version
        probe = Probe(
            name=name,
            mediaType=mediaType,
            system=self.system,
            version=version,
            interval=self.interval,
        )
        self.uow.probes.append(probe)
        return probe

    async def probe(self):
        NotImplemented

    async def run(self):
        identifier = self.__class__.__name__
        logging.info(f"Starting plugin {identifier}")
        try:
            await self._ainit()
        except Exception:
            # XXX log
            logging.info(f"Disabling plugin {identifier}")
            return
        while True:
            logging.info(f"Running probe {identifier} ...")
            try:
                with self.uow:
                    # XXX this seems unclean. Not sure what the flow here
                    # should be
                    await self.probe()
                    await self.uow.commit()
            except Exception:
                logging.exception(f"Error running probe {identifier}")
                # XXX backoff, logging, ...
                pass
            else:
                logging.debug(f"Finished probe {identifier}")
            await asyncio.sleep(self.interval.total_seconds())


class SysctlMacOS(Plugin):

    name = "sysctl"
    mediaType = "vnd.fcio.apple.macos.sysctl"

    async def _ainit(self):
        assert sys.platform == "darwin"
        self.version = await get_current_system_version()

    async def probe(self):
        await self.new_probe().from_command("sysctl", "-a")


class LoadLinux(Plugin):

    name = "load"
    mediaType = "vnd.fcio.linux.load"

    async def _ainit(self):
        assert sys.platform == "linux"
        self.version = await get_current_system_version()

    async def probe(self):
        await self.new_probe().from_file("/proc/loadavg")


class SystemdUnitStates(Plugin):

    async def probe(self):
        self.version = await get_systemd_version()

        # Get general data
        with self.new_probe(
            name="systemd", mediaType=f"vnd.fcio.systemd"
        ) as probe:
            await probe.from_command("systemctl", "show")

        with self.new_probe(
            name="systemd-unit-list", mediaType=f"vnd.fcio.systemd.list-units"
        ) as probe:
            await probe.from_command(
                "systemctl",
                "list-units",
                "--plain",
                "--all",
                "--no-pager",
                "--quiet",
            )
            unit_list = [
                unit.split()[0] for unit in probe.content.splitlines()
            ]
        for unit in unit_list:
            with self.new_probe(
                name=unit, mediaType=f"vnd.fcio.systemd.unit"
            ) as probe:
                await probe.from_command(
                    "systemctl", "show", unit, "--no-pager", "--quiet"
                )


class Keepalive(Plugin):

    name = "keepalive"
    mediaType = "vnd.fcio.keepalive"

    async def _ainit(self):
        self.version = version("aramaki")

    async def probe(self):
        with self.new_probe() as probe:
            probe.content = "keepalive"
            probe.status = 0


def main():
    logging.basicConfig(level=logging.DEBUG)

    loop = asyncio.new_event_loop()

    system = socket.gethostname()
    workdir = pathlib.Path("/var/lib/aramaki/probes")
    interval = datetime.timedelta(seconds=30)

    for plugin_factory in [
        LoadLinux,
        Keepalive,
        SysctlMacOS,
        SystemdUnitStates,
    ]:
        uow = UnitOfWork(workdir)
        plugin = plugin_factory(uow, system, interval)

        loop.create_task(
            plugin.run(),
            name=plugin_factory.__name__,
        )

    try:
        loop.run_forever()
    finally:
        loop.close()
