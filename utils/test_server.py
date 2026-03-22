import asyncio
import json
import math
import time
from websockets.server import serve

HOST = "0.0.0.0"
PORT = 8765
PUBLISH_HZ = 2.0  # messages per second

clients = set()

TRANSCRIPTIONS = [
    " These are some test messages...",
    " Testing: one, two, three...",
    " This is a debug WebSocket message.",
    " The localization vector is changing over time.",
    " Quest App WebSocket test is running."
]


def make_payload(seq: int) -> dict:
    t = time.time()

    payload = {
        "localization": [
            2 * math.cos(t/6),
            2 * math.sin(t/6),
            1
        ],
        "transcription": TRANSCRIPTIONS[seq % len(TRANSCRIPTIONS)]
    }
    return payload


async def handler(websocket):
    clients.add(websocket)
    print(f"Client connected. Total clients: {len(clients)}")

    try:
        async for message in websocket:
            print(f"Received from client: {message}")
    except Exception as e:
        print(f"Client error: {e}")
    finally:
        clients.discard(websocket)
        print(f"Client disconnected. Total clients: {len(clients)}")


async def publisher():
    seq = 0
    period = 1.0 / PUBLISH_HZ

    while True:
        if clients:
            payload = make_payload(seq)
            message = json.dumps(payload)

            dead_clients = set()
            for ws in clients:
                try:
                    await ws.send(message)
                except Exception as e:
                    print(f"Send failed: {e}")
                    dead_clients.add(ws)

            for ws in dead_clients:
                clients.discard(ws)

            print(f"Published: {message}")
            seq += 1

        await asyncio.sleep(period)


async def main():
    async with serve(handler, HOST, PORT):
        print(f"WebSocket server listening on ws://{HOST}:{PORT}")
        print("For Quest, connect to: ws://10.0.0.211:8765")
        await publisher()


if __name__ == "__main__":
    asyncio.run(main())