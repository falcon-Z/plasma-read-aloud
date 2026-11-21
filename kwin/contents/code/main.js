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
        return workspace.windowAt(point, -1).find(window => !window.outline && !window.onScreenDisplay)
            || null;
    }
    const stacking = workspace.stackingOrder;
    for (let i = stacking.length - 1; i >= 0; --i) {
        const window = stacking[i];
        if (window.deleted || window.hidden || window.minimized || window.outline || window.onScreenDisplay
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

function read(method, target) {
    const active = workspace.activeWindow;
    if (target === "focus") {
        send(method, JSON.stringify({target, active: describe(active)}));
        return;
    }
    const point = workspace.cursorPos;
    const screen = workspace.screenAt(point);
    send(method, JSON.stringify({
        target,
        point: [Math.round(point.x), Math.round(point.y)],
        screen: screen ? geometry(screen.geometry) : null,
        desktop: geometry(workspace.virtualScreenGeometry),
        active: describe(active),
        hovered: describe(windowAt(point))
    }));
}

function shortcut(label, keys, activated) {
    const name = "Select to Speak: " + label;
    registerShortcut(name, name, keys, action => {
        action.autoRepeat = false;
        activated();
    });
}

shortcut("Read aloud", "Ctrl+Esc", () => read("ReadOrStop", "auto"));
shortcut("Read from pointer", "Ctrl+Alt+R", () => read("Read", "pointer"));
shortcut("Read keyboard focus", "Ctrl+Alt+Return", () => read("Read", "focus"));
shortcut("Pause or resume", "Ctrl+Alt+Space", () => send("TogglePause"));
shortcut("Previous sentence", "Ctrl+Alt+H", () => send("PreviousSentence"));
shortcut("Next sentence", "Ctrl+Alt+K", () => send("NextSentence"));
// KWin triggers modifier-only shortcuts on release after a standalone tap.
shortcut("Stop playback", "Control", () => send("Stop"));
