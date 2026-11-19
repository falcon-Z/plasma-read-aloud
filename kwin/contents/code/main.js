const service = "org.kde.SelectToSpeak";
const path = "/SelectToSpeak";

function describe(window) {
    if (!window) {
        return null;
    }
    function geometry(rect) {
        return [rect.x, rect.y, rect.width, rect.height].map(Math.round);
    }
    return {
        pid: window.pid,
        title: window.caption,
        frame: geometry(window.frameGeometry),
        client: geometry(window.clientGeometry),
        buffer: geometry(window.bufferGeometry)
    };
}

function windowAt(point) {
    const stacking = workspace.stackingOrder;
    for (let i = stacking.length - 1; i >= 0; --i) {
        const window = stacking[i];
        if (window.deleted || window.minimized || window.pid <= 0 || window.skipSwitcher
            || (window.desktops.length && !window.desktops.includes(workspace.currentDesktop))
            || (window.activities.length && !window.activities.includes(workspace.currentActivity))) {
            continue;
        }
        const rect = window.frameGeometry;
        if (point.x >= rect.x && point.y >= rect.y
            && point.x < rect.x + rect.width && point.y < rect.y + rect.height) {
            return window;
        }
    }
    return null;
}

function send(method, ...args) {
    callDBus(service, path, service, method, ...args);
}

function read() {
    const point = workspace.cursorPos;
    send("ReadOrStop", JSON.stringify({
        point: [Math.round(point.x), Math.round(point.y)],
        active: describe(workspace.activeWindow),
        hovered: describe(windowAt(point))
    }));
}

registerShortcut("Select to Speak: Read or stop", "Select to Speak: Read or stop",
    "Alt+Esc", read);
// KWin triggers a bare modifier on release and leaves Ctrl combinations alone.
registerShortcut("Select to Speak: Stop with Ctrl", "Select to Speak: Stop with Ctrl",
    "Control", () => send("Stop"));
