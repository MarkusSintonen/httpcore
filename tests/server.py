import asyncio

from aiohttp import web

PORT = 1234
RESP = "a" * 2000
SLEEP = 0.01


async def handle(_request):
    await asyncio.sleep(SLEEP)
    return web.Response(text=RESP)


def main() -> None:
    app = web.Application()
    app.add_routes([web.get("/req", handle)])
    web.run_app(app, host="localhost", port=PORT)


if __name__ == "__main__":
    main()
