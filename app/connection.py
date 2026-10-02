from __future__ import annotations

from pathlib import Path
from typing import Any

from nicegui import core, ui


HEARTBEAT_INTERVAL = 25
HEARTBEAT_TIMEOUT = 45


def configure_connection(app: Any) -> None:
    # Establish the proxy-compatible transport first. A failed WebSocket upgrade
    # then leaves polling usable instead of retrying a blocked WebSocket forever.
    app.config.socket_io_js_transports = ["polling", "websocket"]
    script = Path(__file__).with_name("ui").joinpath("connection.js").read_text()
    if "/* st-resilient-connection */" not in app.config.vue_config_script:
        app.config.vue_config_script += "\n" + script

    def heartbeat_policy() -> None:
        # NiceGUI derives these from reconnect_timeout at startup. Keep the
        # heartbeat below common proxy idle limits while retaining pages longer.
        core.sio.eio.ping_interval = HEARTBEAT_INTERVAL
        core.sio.eio.ping_timeout = HEARTBEAT_TIMEOUT
        # Engine.IO's ASGI adapter emits mixed-case encoding headers which the
        # outer GZipMiddleware does not recognize. Use only the outer compressor
        # so polling messages remain decodable through ordinary HTTP proxies.
        core.sio.eio.http_compression = False

    app.on_startup(heartbeat_policy)


def install_connection_status() -> None:
    client = ui.context.client
    client.st_page_visible = True
    install_overlapping_socket_guard(client)

    def visibility(event: Any) -> None:
        if isinstance(event.args, dict) and isinstance(event.args.get("visible"), bool):
            client.st_page_visible = event.args["visible"]

    ui.on("st_page_visibility", visibility)


def install_overlapping_socket_guard(client: Any) -> None:
    """An old transport timing out must not disconnect its newer replacement.

    NiceGUI 3.13 clears tab_id for every socket disconnect, even if a reconnect
    already registered another socket for the same page. Its event dispatcher
    then silently ignores input and its outbox stops sending despite a live
    connection. Retain the most recently handshaken tab while sockets remain.
    """
    last_tab_id = None

    def connected() -> None:
        nonlocal last_tab_id
        if client.tab_id is not None:
            last_tab_id = client.tab_id

    def disconnected() -> None:
        if getattr(client, "_socket_to_document_id", {}) and last_tab_id is not None:
            client.tab_id = last_tab_id

    client.on_connect(connected)
    client.on_disconnect(disconnected)
