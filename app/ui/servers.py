from __future__ import annotations

from nicegui import ui
from sqlalchemy import Engine

from app.db import engine, session_scope
from app.models import ProjectServer, Server
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


def remove_server(server_id: int, *, target_engine: Engine = engine) -> bool:
    """Remove a server from active management.

    The server row is kept as disabled so historical runs can still show the server alias.
    Project links are removed so it disappears from project launch/server lists.
    """

    with session_scope(target_engine) as session:
        server = session.get(Server, server_id)
        if server is None:
            _notify("Server not found.", type="negative")
            return False
        alias = server.alias
        for link in session.query(ProjectServer).filter_by(server_id=server_id).all():
            session.delete(link)
        server.enabled = False

    _notify(f"Server '{alias}' removed.", type="positive")
    ui.navigate.reload()
    return True


def _load_managed_servers(*, target_engine: Engine = engine) -> list[Server]:
    with session_scope(target_engine) as session:
        return list(
            session.query(Server)
            .filter(Server.enabled.is_(True))
            .order_by(Server.alias)
            .all()
        )


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


def import_servers(aliases: list[str], *, target_engine: Engine = engine) -> int:
    """Import a chosen set in one transaction and refresh the page once."""
    clean_aliases = sorted({alias.strip() for alias in aliases if alias.strip()})
    if not clean_aliases:
        _notify("请至少选择一台机器。", type="warning")
        return 0
    with session_scope(target_engine) as session:
        for alias in clean_aliases:
            server = session.query(Server).filter_by(alias=alias).one_or_none()
            if server is None:
                session.add(Server(alias=alias, name=alias, enabled=True))
            else:
                server.enabled = True
    _notify(f"已导入 {len(clean_aliases)} 台机器。", type="positive")
    ui.navigate.reload()
    return len(clean_aliases)


def _dialog_owner():
    # A page-owned container keeps dialogs alive independently of list refreshes.
    with ui.context.client.content:
        return ui.element("div")


def _open_server_editor(server: Server | None = None) -> None:
    owner = _dialog_owner()
    with owner, ui.dialog() as dialog, ui.card().classes("st-dialog"):
        ui.label("编辑机器" if server else "添加机器").classes("st-dialog-title")
        alias = ui.input("SSH alias", value=server.alias if server else "").props("outlined dense")
        if server:
            alias.disable()
            ui.label("SSH alias 用于关联历史任务；可在下方修改显示名称。").classes("st-dialog-body")
        name = ui.input("显示名称", value=server.name if server else "").props("outlined dense")
        with ui.row().classes("st-actions"):
            ui.button("取消", on_click=dialog.close).props("flat")
            ui.button(
                "保存", color=None,
                on_click=lambda: add_server(alias.value or "", name.value or "")
            ).classes("st-primary").props("unelevated")
    dialog.on("hide", owner.delete)
    dialog.open()


def _open_remove_dialog(server: Server) -> None:
    owner = _dialog_owner()
    with owner, ui.dialog() as dialog, ui.card().classes("st-dialog"):
        ui.label("移除机器").classes("st-dialog-title")
        ui.label(server.alias).classes("st-mono st-manage-name")
        ui.label(
            "将从监控列表移除这台机器，同时移除项目中的机器关联及工作目录配置。"
            "历史运行记录仍会保留，远端任务不会停止。"
        ).classes("st-dialog-body")
        with ui.row().classes("st-actions"):
            ui.button("取消", on_click=dialog.close).props("flat")
            ui.button(
                "确认移除", on_click=lambda: remove_server(int(server.id)), color="negative"
            ).props("unelevated")
    dialog.on("hide", owner.delete)
    dialog.open()


def _open_import_dialog(managed_aliases: set[str]) -> None:
    candidates = _candidate_aliases(managed_aliases)
    selected: dict[str, bool] = {}
    owner = _dialog_owner()
    with owner, ui.dialog() as dialog, ui.card().classes("st-dialog"):
        ui.label("从 SSH config 导入").classes("st-dialog-title")
        ui.label("选择要纳入监控的 SSH 别名。").classes("st-dialog-body")
        if not candidates:
            ui.label("没有发现尚未添加的机器。").classes("st-muted")
        with ui.column().classes("st-import-options"):
            for alias in candidates:
                ui.checkbox(alias, value=False).bind_value(selected, alias)
        with ui.row().classes("st-actions"):
            ui.button("取消", on_click=dialog.close).props("flat")
            submit = ui.button(
                "导入所选机器",
                color=None,
                on_click=lambda: import_servers([alias for alias, enabled in selected.items() if enabled]),
            ).classes("st-primary").props("unelevated")
            if not candidates:
                submit.disable()
    dialog.on("hide", owner.delete)
    dialog.open()


def _server_card(server: Server) -> None:
    with ui.row().classes("st-manage-row"):
        with ui.column().classes("st-manage-identity"):
            ui.label(server.name or server.alias).classes("st-manage-name")
            if server.name and server.name != server.alias:
                ui.label(server.alias).classes("st-mono st-muted")
            with ui.row().classes("st-manage-status"):
                ui.icon("check_circle")
                ui.label("已启用 · 使用本机 SSH 配置")
        with ui.row().classes("st-actions"):
            ui.button("测试连接", color=None, on_click=_test_button_handler(server.alias)).props(
                "outline icon=lan"
            ).classes("st-secondary")
            ui.button("编辑", on_click=lambda: _open_server_editor(server)).props("flat")
            ui.button("移除", on_click=lambda: _open_remove_dialog(server)).props("flat color=grey-7")


def render_servers_page() -> None:
    """Keep managed machines central; move add, import and edits to dialogs."""
    managed = _load_managed_servers()
    managed_aliases = {server.alias for server in managed}
    with ui.row().classes("st-page-header"):
        with ui.column().classes("gap-0"):
            ui.label("机器管理").classes("st-page-title")
            ui.label(f"{len(managed)} 台已启用 · SSH 连接与显示名称").classes("st-page-subtitle")
        with ui.row().classes("st-actions"):
            ui.button(
                "从 SSH config 导入", color=None,
                on_click=lambda: _open_import_dialog(managed_aliases)
            ).props("outline icon=file_upload").classes("st-secondary")
            ui.button("添加机器", color=None, on_click=lambda: _open_server_editor()).props(
                "unelevated icon=add"
            ).classes("st-primary")
    with ui.card().classes("st-panel st-manage-list"):
        if not managed:
            with ui.column().classes("st-empty"):
                ui.icon("dns").classes("text-3xl")
                ui.label("还没有配置机器")
                ui.label("添加 SSH alias，或从 SSH config 中选择导入。")
        for server in managed:
            _server_card(server)
