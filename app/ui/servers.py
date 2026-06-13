from __future__ import annotations

from nicegui import ui
from sqlalchemy import Engine

from app.db import engine, session_scope
from app.models import Server
from app.ssh_client import SSHClient, scan_ssh_config_hosts
from app.visual_actions import test_connection


def _notify(message: str, *, type: str = "info") -> None:
    ui.notify(message, type=type)


def add_server(alias: str, name: str = "", *, target_engine: Engine = engine) -> bool:
    """Create or update a managed server by SSH alias."""

    clean_alias = alias.strip()
    clean_name = name.strip() or clean_alias
    if not clean_alias:
        _notify("SSH alias is required.", type="negative")
        return False

    with session_scope(target_engine) as session:
        server = session.query(Server).filter_by(alias=clean_alias).one_or_none()
        if server is None:
            server = Server(alias=clean_alias)
            session.add(server)
        server.name = clean_name
        server.enabled = True

    _notify(f"Server '{clean_alias}' saved.", type="positive")
    ui.navigate.reload()
    return True


def _load_managed_servers(*, target_engine: Engine = engine) -> list[Server]:
    with session_scope(target_engine) as session:
        return list(session.query(Server).order_by(Server.alias).all())


def _candidate_aliases(managed_aliases: set[str]) -> list[str]:
    return [alias for alias in scan_ssh_config_hosts() if alias not in managed_aliases]


async def _test_server(alias: str) -> None:
    try:
        ok, message = await test_connection(alias, SSHClient())
    except Exception as exc:  # pragma: no cover - defensive around real SSH callbacks
        _notify(f"{alias}: {exc}", type="negative")
        return
    if ok:
        _notify(f"{alias}: connection ok", type="positive")
    else:
        _notify(f"{alias}: {message}", type="negative")


def _test_button_handler(alias: str):
    async def handler() -> None:
        await _test_server(alias)

    return handler


def _server_card(server: Server) -> None:
    status = "enabled" if server.enabled else "disabled"
    status_class = "text-positive" if server.enabled else "text-grey-6"
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between w-full"):
            with ui.column().classes("gap-1"):
                ui.label(server.alias).classes("text-lg font-semibold")
                ui.label(server.name or server.alias).classes("text-grey-7")
                ui.label(status).classes(status_class)
            ui.button("Test", on_click=_test_button_handler(server.alias))


def render_servers_page() -> None:
    """Render the managed servers administration page."""

    ui.label("Servers").classes("text-2xl font-bold")
    ui.label("Add SSH aliases, import hosts from your SSH config, and test connectivity.").classes(
        "text-grey-7"
    )

    alias_input = ui.input("SSH alias").classes("w-full")
    name_input = ui.input("Display name (optional)").classes("w-full")
    ui.button(
        "Add Server",
        on_click=lambda: add_server(alias_input.value or "", name_input.value or ""),
    )

    managed = _load_managed_servers()
    managed_aliases = {server.alias for server in managed}
    candidates = _candidate_aliases(managed_aliases)

    ui.separator()
    ui.label("SSH config candidates").classes("text-xl font-semibold")
    if not candidates:
        ui.label("No new SSH config hosts found.").classes("text-grey-7")
    else:
        for candidate in candidates:
            with ui.row().classes("items-center gap-3"):
                ui.label(candidate).classes("font-mono")
                ui.button("Import", on_click=lambda alias=candidate: add_server(alias))

    ui.separator()
    ui.label("Managed servers").classes("text-xl font-semibold")
    if not managed:
        ui.label("No managed servers yet.").classes("text-grey-7")
        return

    for server in managed:
        _server_card(server)
