from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app import connection


SCRIPT = Path(connection.__file__).with_name("ui") / "connection.js"


NODE_HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
class Emitter {
  constructor() { this.listeners = new Map(); }
  on(name, fn) { const handlers = this.listeners.get(name) || []; handlers.push(fn); this.listeners.set(name, handlers); return this; }
  fire(name, ...args) { for (const fn of [...(this.listeners.get(name) || [])]) fn(...args); }
  addEventListener(name, fn) { this.on(name, fn); }
}
class FakeSocket extends Emitter {
  constructor() { super(); this.connected = false; this.sendBuffer = []; this.sent = []; this.io = new Emitter(); this.connectCalls = 0; }
  fire(name, ...args) {
    // Socket.IO flushes queued packets before notifying connect listeners.
    if (name === 'connect') this.sent.push(...this.sendBuffer.splice(0));
    return super.fire(name, ...args);
  }
  emit(name, ...args) {
    const packet = { data: [name, ...args] };
    if (this.connected) this.sent.push(packet); else this.sendBuffer.push(packet);
    return this;
  }
  connect() { this.connectCalls += 1; return this; }
}
const document = new Emitter();
document.visibilityState = 'visible';
document.body = { dataset: {}, children: [], append(node) { this.children.push(node); } };
document.head = { children: [], append(node) { this.children.push(node); } };
document.createElement = tag => ({ tag, hidden: false, textContent: '', attributes: {}, setAttribute(k, v) { this.attributes[k] = v; } });
const window = new Emitter();
const navigator = { onLine: true };
const timers = new Map();
const deferred = [];
let timerId = 0;
let capturedOptions;
let rawSocket;
window.did_handshake = false;
window.io = (_url, options) => { capturedOptions = options; return rawSocket = new FakeSocket(); };
window.io.protocol = 5;
const visibilityReports = [];
window.emitEvent = (name, payload) => {
  visibilityReports.push([name, payload]);
  window.socket.emit('event', { listener_id: 'visibility', args: payload });
};
const context = vm.createContext({
  window, document, navigator, console,
  setTimeout(fn) { deferred.push(fn); return deferred.length; },
  setInterval(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
  clearInterval(id) { timers.delete(id); },
});
vm.runInContext(input.source, context);
window.socket = window.io('http://test.invalid', { transports: ['polling', 'websocket'], query: { client_id: 'test' } });
const socket = window.socket;
const banner = document.body.children.find(item => item.id === 'st-connection-status');
function flush() { while (deferred.length) deferred.shift()(); }
function tick() { for (const timer of [...timers.values()]) timer.fn(); }
function connect() { socket.connected = true; socket.fire('connect'); window.did_handshake = true; flush(); tick(); }
function disconnect() { socket.connected = false; socket.fire('disconnect'); }
function interaction(type, kind = 'button', options = {}) {
  const control = { matches: selector => kind === 'input' && selector.includes('input') };
  const event = { type, key: '', target: { closest: selector => selector.includes(kind) ? control : null }, prevented: false, stopped: false,
    preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; }, ...options };
  document.fire(type, event);
  return event;
}
const environment = { assert, window, document, navigator, socket, banner, timers, capturedOptions, visibilityReports,
  connect, disconnect, flush, tick, interaction };
vm.runInNewContext(input.scenario, environment);
"""


def run_script(scenario: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser connection policy checks")
    result = subprocess.run(
        [node, "-e", NODE_HARNESS],
        input=json.dumps({"source": SCRIPT.read_text(), "scenario": scenario}),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_transport_and_heartbeat_policy_runs_after_nicegui_startup(monkeypatch):
    handlers = []
    app = SimpleNamespace(
        config=SimpleNamespace(socket_io_js_transports=[], vue_config_script="existing_setup();"),
        on_startup=handlers.append,
    )
    eio = SimpleNamespace(ping_interval=240, ping_timeout=120)
    monkeypatch.setattr(connection, "core", SimpleNamespace(sio=SimpleNamespace(eio=eio)))

    connection.configure_connection(app)

    assert app.config.socket_io_js_transports == ["polling", "websocket"]
    assert app.config.vue_config_script.startswith("existing_setup();")
    assert SCRIPT.read_text() in app.config.vue_config_script
    assert len(handlers) == 1
    handlers[0]()
    assert eio.ping_interval == 25
    assert eio.ping_timeout == 45
    assert eio.http_compression is False


@pytest.mark.asyncio
async def test_polling_update_has_one_decodable_compression_layer(monkeypatch):
    import asyncio
    import hashlib
    import engineio
    import httpx
    from starlette.middleware.gzip import GZipMiddleware

    server = engineio.AsyncServer(async_mode="asgi", monitor_clients=False)
    handlers = []
    app = SimpleNamespace(config=SimpleNamespace(vue_config_script=""), on_startup=handlers.append)
    monkeypatch.setattr(connection, "core", SimpleNamespace(sio=SimpleNamespace(eio=server)))
    connection.configure_connection(app)
    handlers[0]()
    transport = httpx.ASGITransport(app=GZipMiddleware(engineio.ASGIApp(server)))
    # Incompressible enough to exceed both Engine.IO and outer GZip thresholds.
    payload = "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(500))
    tasks_before = set(asyncio.all_tasks())
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            opened = await client.get("/engine.io/?EIO=4&transport=polling", headers={"accept-encoding": "gzip"})
            sid = json.loads(opened.text[1:])["sid"]
            await server.send(sid, payload)
            response = await client.get(f"/engine.io/?EIO=4&transport=polling&sid={sid}",
                                        headers={"accept-encoding": "gzip"})
            assert response.headers.get_list("content-encoding") == ["gzip"]
            assert response.text == "4" + payload
            await server.sockets[sid].close(wait=False, abort=True)
    finally:
        await server.shutdown()
        pending = set(asyncio.all_tasks()) - tasks_before
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def test_visibility_event_requires_a_boolean_and_updates_only_its_client(monkeypatch):
    client = SimpleNamespace(on_connect=lambda _handler: None, on_disconnect=lambda _handler: None)
    handlers = {}
    fake_ui = SimpleNamespace(context=SimpleNamespace(client=client), on=lambda name, fn: handlers.setdefault(name, fn))
    monkeypatch.setattr(connection, "ui", fake_ui)
    connection.install_connection_status()
    assert client.st_page_visible is True
    handler = handlers["st_page_visibility"]
    for payload in (None, [], {"visible": "false"}, {"visible": 0}, {}):
        handler(SimpleNamespace(args=payload))
        assert client.st_page_visible is True
    handler(SimpleNamespace(args={"visible": False}))
    assert client.st_page_visible is False
    handler(SimpleNamespace(args={"visible": True}))
    assert client.st_page_visible is True


@pytest.mark.asyncio
async def test_old_socket_disconnect_keeps_reconnected_page_interactive(monkeypatch):
    import asyncio
    from nicegui import ui, core
    from nicegui.client import Client
    from nicegui.nicegui import _on_event
    from starlette.requests import Request

    monkeypatch.setattr(core, "loop", asyncio.get_running_loop())
    client = Client(ui.page("/weaknet-test", reconnect_timeout=300),
                    request=Request({"type": "http", "path": "/weaknet-test", "headers": []}))
    clicks = []
    try:
        connection.install_overlapping_socket_guard(client)
        with client:
            button = ui.button("Action", on_click=lambda: clicks.append("handled"))
        client.tab_id = "retained-tab"
        client.handle_handshake("old-socket", "same-document", None)
        client.tab_id = "retained-tab"
        client.handle_handshake("new-socket", "same-document", None)
        client.handle_disconnect("old-socket")
        assert client.has_socket_connection
        assert client.tab_id == "retained-tab"
        _on_event("new-socket", {"client_id": client.id, "id": button.id,
                  "listener_id": next(iter(button._event_listeners)), "args": []})
        assert clicks == ["handled"]
        client.handle_disconnect("new-socket")
        assert not client.has_socket_connection
    finally:
        client.delete()
        await asyncio.sleep(0)


def test_client_uses_bounded_http_requests_without_manager_reload_timeout():
    run_script(r"""
assert.equal(capturedOptions.timeout, false);
assert.equal(capturedOptions.requestTimeout, 45000);
assert.equal(capturedOptions.reconnection, true);
assert.equal(capturedOptions.reconnectionDelayMax, 10000);
assert.deepEqual(Array.from(capturedOptions.transports), ['polling', 'websocket']);
assert.equal(capturedOptions.query.client_id, 'test');
assert.equal(window.io.protocol, 5);
assert.equal(timers.size, 1);
assert.equal([...timers.values()][0].ms, 250);
for (let i = 0; i < 8; i++) tick();
assert.equal(socket.sent.length, 0);
assert.equal(socket.sendBuffer.length, 0);
""")


def test_offline_actions_are_not_buffered_or_replayed_after_reconnect():
    run_script(r"""
connect();
socket.sent.length = 0;
socket.sendBuffer.push({data: ['event', {action: 'launch'}]}, {data: ['ack', {next_message_id: 3}]});
disconnect();
assert.deepEqual(Array.from(socket.sendBuffer, p => p.data[0]), ['ack']);
const click = interaction('click');
assert.equal(click.prevented, true);
assert.equal(click.stopped, true);
assert.equal(interaction('submit', 'form').prevented, true);
assert.equal(banner.hidden, false);
socket.emit('event', {action: 'stop'});
assert.deepEqual(Array.from(socket.sendBuffer, p => p.data[0]), ['ack']);
assert.equal(socket.sent.length, 0);
socket.sendBuffer.push({data: ['event', {action: 'launch'}]});
socket.io.fire('reconnect_attempt');
assert.deepEqual(Array.from(socket.sendBuffer, p => p.data[0]), ['ack']);
connect();
assert.equal(banner.hidden, true);
assert.equal(interaction('beforeinput', 'input').prevented, false);
socket.emit('event', {action: 'edit', value: 'new value'});
assert.equal(socket.sent.at(-1).data[1].value, 'new value');
assert.equal(socket.sent.some(p => ['stop', 'launch'].includes(p.data[1]?.action)), false);
""")


def test_explicit_reconnect_handshake_must_finish_before_inputs_are_allowed():
    run_script(r"""
connect();
disconnect();
assert.equal(window.did_handshake, false);
window.did_handshake = true; // Simulate a stale library flag from the prior connection.
socket.connected = true;
socket.fire('connect');
flush(); tick();
assert.equal(window.did_handshake, false);
assert.equal(document.body.dataset.stConnection, 'offline');
assert.equal(interaction('beforeinput', 'input').prevented, true);
window.did_handshake = true;
tick();
assert.equal(document.body.dataset.stConnection, 'online');
assert.equal(interaction('beforeinput', 'input').prevented, false);
""")


def test_visibility_reports_only_changes_and_reports_current_state_after_reconnect():
    run_script(r"""
connect();
assert.deepEqual(Array.from(visibilityReports, p => p[1].visible), [true]);
tick(); tick();
assert.equal(visibilityReports.length, 1);
document.visibilityState = 'hidden';
document.fire('visibilitychange');
assert.deepEqual(Array.from(visibilityReports, p => p[1].visible), [true, false]);
disconnect();
document.visibilityState = 'visible';
document.fire('visibilitychange');
assert.equal(visibilityReports.length, 2);
connect();
assert.deepEqual(Array.from(visibilityReports, p => p[1].visible), [true, false, true]);
""")


def test_browser_offline_event_does_not_destroy_an_existing_completed_handshake():
    run_script(r"""
connect();
navigator.onLine = false;
window.fire('offline');
assert.equal(document.body.dataset.stConnection, 'offline');
assert.equal(interaction('click').prevented, true);
const sentCount = socket.sent.length;
socket.emit('event', {action: 'launch'});
assert.equal(socket.sent.length, sentCount);
assert.equal(socket.sendBuffer.length, 0);
assert.equal(window.did_handshake, true);
navigator.onLine = true;
window.fire('online');
assert.equal(document.body.dataset.stConnection, 'online');
assert.equal(socket.connectCalls, 0);
assert.equal(interaction('click').prevented, false);
""")


def test_offline_copy_and_back_forward_cache_restore_remain_usable():
    run_script(r"""
connect();
disconnect();
assert.equal(interaction('keydown', 'input', {key: 'c', ctrlKey: true}).prevented, false);
assert.equal(interaction('keydown', 'input', {key: 'a', metaKey: true}).prevented, false);
assert.equal(interaction('keydown', 'input', {key: 'Tab'}).prevented, false);
assert.equal(interaction('keydown', 'input', {key: 'x'}).prevented, true);
window.fire('pagehide', {persisted: true});
assert.equal(timers.size, 0);
window.fire('pageshow', {persisted: true});
assert.equal(timers.size, 1);
window.fire('pageshow', {persisted: true});
assert.equal(timers.size, 1);
connect();
assert.equal(document.body.dataset.stConnection, 'online');
""")
