const service = "org.kde.SelectToSpeak";
const path = "/SelectToSpeak";

function geometry(rect) {
    return [rect.x, rect.y, rect.width, rect.height].map(Math.round);
}

function describe(window) {
    if (!window) {
        return null;
    }
    return {
        pid: window.pid,
        title: window.caption,
        frame: geometry(window.frameGeometry),
        client: geometry(window.clientGeometry),
        buffer: geometry(window.bufferGeometry),
        popup: Boolean(window.popupWindow || window.popupMenu || window.tooltip)
    };
}

function windowAt(point) {
    if (typeof workspace.windowAt === "function") {
        return workspace.windowAt(point, 1)[0] || null;
    }
    const stacking = workspace.stackingOrder;
    for (let i = stacking.length - 1; i >= 0; --i) {
        const window = stacking[i];
        if (window.deleted || window.hidden || window.minimized || window.outline
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

function readOrStop(action) {
    action.autoRepeat = false;
    const point = workspace.cursorPos;
    const screen = workspace.screenAt(point);
    send("ReadOrStop", JSON.stringify({
        point: [Math.round(point.x), Math.round(point.y)],
        screen: screen ? geometry(screen.geometry) : null,
        desktop: geometry(workspace.virtualScreenGeometry),
        active: describe(workspace.activeWindow),
        hovered: describe(windowAt(point))
    }));
}

registerShortcut("Select to Speak: Read aloud", "Select to Speak: Read aloud",
    "Ctrl+Esc", readOrStop);
// The reader binds Escape to this action only while a request is active.
registerShortcut("Select to Speak: Stop reading", "Select to Speak: Stop reading",
    "", () => send("Stop"));
