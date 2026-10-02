/* st-resilient-connection */
(() => {
  // Executed by NiceGUI's vue_config_script before app.mount opens the socket.
  const originalIo = window.io;
  let socket;
  let ready = false;
  let lastVisibility;
  const banner = document.createElement("div");
  banner.id = "st-connection-status";
  banner.setAttribute("role", "status");
  banner.setAttribute("aria-live", "polite");
  banner.hidden = true;
  document.body.append(banner);
  const style = document.createElement("style");
  style.textContent = `
    #popup { display: none !important; }
    #st-connection-status { position:fixed; bottom:16px; left:50%; transform:translateX(-50%);
      z-index:100000; max-width:calc(100vw - 32px); width:max-content; padding:10px 16px;
      border:1px solid #e6d5ab; border-radius:8px; background:#fff9eb; color:#715725;
      font:13px/1.6 system-ui,sans-serif; box-shadow:0 2px 12px #0000000a; pointer-events:none; }
    #st-connection-status[hidden] { display:none; }
    body[data-st-connection="offline"] .q-btn { opacity:.6; cursor:wait; }
  `;
  document.head.append(style);

  function usable() {
    return Boolean(ready && socket?.connected && window.did_handshake && navigator.onLine !== false);
  }
  function showDisconnected() {
    ready = false;
    lastVisibility = undefined;
    document.body.dataset.stConnection = "offline";
    banner.textContent = "连接中断 · 当前画面已保留，可查看和复制。正在自动重连，恢复后再执行操作。";
    banner.hidden = false;
  }
  function dropPendingActions() {
    // Never replay a launch/stop/edit that was clicked after losing connection.
    if (socket?.sendBuffer) {
      socket.sendBuffer = socket.sendBuffer.filter(packet => packet.data?.[0] !== "event");
    }
  }
  function reportVisibility() {
    const visible = document.visibilityState !== "hidden";
    if (!usable() || visible === lastVisibility) return;
    lastVisibility = visible;
    window.emitEvent("st_page_visibility", { visible });
  }
  function updateReadiness() {
    // NiceGUI completes its explicit handshake after Socket.IO's connect event.
    if (!socket?.connected || !window.did_handshake || navigator.onLine === false) return;
    ready = true;
    document.body.dataset.stConnection = "online";
    banner.hidden = true;
    reportVisibility();
  }

  window.io = Object.assign(function (url, options) {
    socket = originalIo(url, {
      ...options,
      // A manager "timeout" makes NiceGUI reload the whole page. Instead let
      // failed HTTP attempts report transport errors and reconnect in place.
      timeout: false,
      requestTimeout: 45000,
      reconnection: true,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 10000,
      randomizationFactor: 0.5,
    });
    const emit = socket.emit;
    socket.emit = function (event, ...args) {
      if (event === "event" && !usable()) return this;
      return emit.call(this, event, ...args);
    };
    socket.on("disconnect", () => {
      // NiceGUI leaves this true after disconnect. An explicit handshake on
      // the next connection must finish before events are allowed again.
      window.did_handshake = false;
      dropPendingActions();
      showDisconnected();
    });
    socket.on("connect_error", showDisconnected);
    socket.on("connect", () => {
      ready = false;
      window.did_handshake = false;
      // The library's handshake runs in its own event handler after this one.
      setTimeout(updateReadiness, 0);
    });
    socket.io.on("reconnect_attempt", dropPendingActions);
    return socket;
  }, originalIo);

  const controlSelector = "button, [role=button], a[href], form, input, textarea, select, [role=combobox], [contenteditable=true]";
  function guardInteraction(event) {
    if (usable()) return;
    const control = event.target?.closest?.(controlSelector);
    if (!control) return;
    // Selection, scrolling and copying remain local even while disconnected.
    if (event.type === "keydown") {
      if (event.key === "Tab" || event.key === "Escape") return;
      if ((event.ctrlKey || event.metaKey) && ["a", "c"].includes(event.key.toLowerCase())) return;
      if (!control.matches("input,textarea,[contenteditable=true]") && !["Enter", " "].includes(event.key)) return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    showDisconnected();
  }
  for (const event of ["click", "submit", "beforeinput", "keydown", "change"]) {
    document.addEventListener(event, guardInteraction, true);
  }
  window.addEventListener("offline", () => { dropPendingActions(); showDisconnected(); });
  window.addEventListener("online", () => {
    if (socket && !socket.connected) socket.connect();
    updateReadiness();
  });
  document.addEventListener("visibilitychange", reportVisibility);
  // Local-only readiness check: no heartbeat, request or server round trip.
  let readinessTimer;
  function startReadinessTimer() {
    if (readinessTimer === undefined) readinessTimer = setInterval(updateReadiness, 250);
    updateReadiness();
  }
  startReadinessTimer();
  window.addEventListener("pagehide", () => {
    clearInterval(readinessTimer);
    readinessTimer = undefined;
  });
  // pagehide can suspend rather than destroy a page (the back/forward cache).
  window.addEventListener("pageshow", startReadinessTimer);
})();
