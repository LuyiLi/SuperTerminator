from __future__ import annotations

from nicegui import ui

from app.config import load_settings


def render_settings_page() -> None:
    """Show effective service configuration without implying editable settings."""
    settings = load_settings()
    with ui.row().classes('st-page-header'):
        with ui.column().classes('gap-0'):
            ui.label('工作区设置').classes('st-page-title')
            ui.label('当前服务配置').classes('st-page-subtitle')
    with ui.card().classes('st-panel st-settings-panel'):
        for label, value, mono in [
            ('数据库位置', str(settings.db_path), True),
            ('资源与状态刷新', f'{settings.refresh_seconds} 秒', False),
            ('任务输出刷新', f'{settings.run_output_seconds} 秒', False),
            ('调试终端', '显示' if settings.show_debug_terminal else '隐藏', False),
        ]:
            with ui.element('div').classes('st-setting-row'):
                ui.label(label).classes('st-setting-label')
                ui.label(value).classes('st-setting-value' + (' st-mono' if mono else ''))
    ui.label('通过服务环境配置调整；重启服务后生效。').classes('st-muted')
