import asyncio
import datetime
import glob
import json
import logging
import os
import pathlib
import sys

import aiofiles
import aiofiles.os
import websockets
import zstd

OUTGOING: asyncio.Queue = asyncio.Queue()


async def send_message(websocket, path):

    if path.name.startswith("."):
        return

    if not await aiofiles.os.path.isfile(path):
        return

    logging.debug(path)

    async with aiofiles.open(path, "rb") as f:
        data = await f.read()

    # xxx sign with proper actor keys
    # XXX protect against double processing?
    # XXX protect against out of order processing?
    # XXX use TTL to expire

    await websocket.send(zstd.compress(data, 1))

    await aiofiles.os.unlink(path)


async def send_messages(websocket):
    while True:
        logging.info("Waiting for outgoing messages ... ")
        path = await OUTGOING.get()
        try:
            await send_message(websocket, path)
        except Exception:
            logging.exception("Unexpected error")
        await asyncio.sleep(0)
        OUTGOING.task_done()


async def receive_messages(websocket):
    received = 0
    receive_start = datetime.datetime.now()

    async for message_str in websocket:
        received += 1
        if received % 100:
            logging.debug(
                received
                / (receive_start - datetime.datetime.now()).total_seconds()
            )
        # XXX verify message integrity, authenticity, ...
        message_str = zstd.decompress(message_str)
        message = json.loads(message_str)
        message_id = message["id"]
        logging.debug(message_id)
        # XXX write to inbox (multiple inboxes, one for each actor?)
        async with aiofiles.open(f"work/inbox/.{message_id}.json", "w") as f:
            await f.write(json.dumps(message))

        await aiofiles.os.rename(
            f"work/inbox/.{message_id}.json",
            f"work/inbox/{message_id}.json",
        )


async def client():
    # XXX developer dummy: send data around so that we receive the messages
    # back in the inbox
    logging.info("Starting client")
    uri = "ws://127.0.0.1:8764"
    async for websocket in websockets.connect(uri):
        try:
            await asyncio.gather(
                receive_messages(websocket), send_messages(websocket)
            )
        except websockets.ConnectionClosed:
            continue


async def handle_backfills(path):
    logging.info("Backfilling")
    for file in pathlib.Path(f"/var/lib/aramaki/probes/outbox/").glob(
        "*.json"
    ):
        logging.info(f"Backfill: {file}")
        await OUTGOING.put(file)
    logging.info("Done backfilling")


async def monitor(path="/var/lib/aramaki/probes/outbox/"):
    asyncio.create_task(handle_backfills(path))

    logging.info("Starting fswatch")

    watch = await asyncio.create_subprocess_exec(
        "fswatch",
        path,
        # fmt: off
        "--event", "Renamed",
        "--event", "MovedTo",
        # fmt: on
        stdout=asyncio.subprocess.PIPE,
    )

    logging.info(f"stdout {watch.stdout}")

    assert watch.stdout is not None

    logging.info("waiting for messages")

    while line_raw := await watch.stdout.readline():
        line = line_raw.decode(sys.getfilesystemencoding())
        path = pathlib.Path(line.strip())
        logging.info(f"noticed {path}")
        asyncio.create_task(OUTGOING.put(path))


async def main_():
    asyncio.create_task(monitor())
    asyncio.create_task(client())
    await asyncio.Future()  # run forever


def main():
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger("websockets").setLevel(logging.INFO)
    asyncio.run(main_())
