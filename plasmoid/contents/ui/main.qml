pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.extras as PlasmaExtras
import org.kde.plasma.plasmoid
import org.kde.plasma.workspace.dbus as DBus

PlasmoidItem {
    id: root

    readonly property string serviceName: "org.kde.SelectToSpeak"
    readonly property string objectPath: "/SelectToSpeak"
    readonly property string phase: reader.properties.Phase ?? ""
    readonly property string savedVoice: reader.properties.VoiceId ?? ""
    readonly property real rate: reader.properties.Rate ?? 1.2
    readonly property var voices: JSON.parse(reader.properties.Voices ?? "[]")
    readonly property bool ready: phase !== ""
    readonly property bool previewing: phase === "preparing preview" || phase === "previewing"
    readonly property bool reading: ready && phase !== "idle" && !previewing
    readonly property string statusText: {
        switch (phase) {
        case "idle": return i18n("Ctrl+Esc to read")
        case "finding text": return i18n("Finding text…")
        case "recognizing text": return i18n("Recognizing text…")
        case "preparing audio": return i18n("Preparing audio…")
        case "speaking": return i18n("Reading aloud")
        case "preparing preview": return i18n("Preparing voice preview…")
        case "previewing": return i18n("Playing voice preview")
        default: return i18n("The read-aloud service is unavailable.")
        }
    }
    property string requestError: ""

    Plasmoid.icon: "select-to-speak-symbolic"
    Plasmoid.status: PlasmaCore.Types.ActiveStatus
    toolTipMainText: Plasmoid.title
    toolTipSubText: statusText

    DBus.DBusServiceWatcher {
        id: serviceWatcher
        watchedService: root.serviceName
    }

    DBus.Properties {
        id: reader
        service: serviceWatcher.registered ? root.serviceName : ""
        path: root.objectPath
        iface: root.serviceName
    }

    function callReader(method, args, completed = () => {}) {
        requestError = ""
        DBus.SessionBus.asyncCall({
            service: serviceName,
            path: objectPath,
            iface: serviceName,
            member: method,
            arguments: args
        }, completed, reply => {
            root.requestError = reply.error.message;
        });
    }

    fullRepresentation: PlasmaExtras.Representation {
        id: popup

        property string candidate: ""
        readonly property string voice: candidate || root.savedVoice
        readonly property string errorText: root.requestError || (reader.properties.PreviewError ?? "")

        Layout.minimumWidth: Kirigami.Units.gridUnit * 16
        Layout.preferredWidth: Kirigami.Units.gridUnit * 20
        Layout.minimumHeight: controls.implicitHeight + Kirigami.Units.largeSpacing * 2
        Layout.preferredHeight: Layout.minimumHeight

        header: PlasmaExtras.BasicPlasmoidHeading {}

        contentItem: Item {
            ColumnLayout {
                id: controls
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.margins: Kirigami.Units.largeSpacing
                spacing: Kirigami.Units.smallSpacing

                PlasmaComponents.Label {
                    text: i18n("Voice")
                }

                PlasmaComponents.ComboBox {
                    id: picker

                    implicitWidth: Kirigami.Units.gridUnit * 12
                    Layout.fillWidth: true
                    enabled: root.ready && !root.previewing
                    model: root.voices
                    textRole: "label"
                    valueRole: "id"
                    currentIndex: root.voices.findIndex(voice => voice.id === popup.voice)
                    Accessible.name: i18n("Voice")
                    onActivated: index => {
                        popup.candidate = root.voices[index].id;
                        root.requestError = "";
                    }
                }

                RowLayout {
                    Layout.topMargin: Kirigami.Units.smallSpacing

                    PlasmaComponents.Button {
                        Layout.fillWidth: true
                        text: root.previewing ? i18n("Stop preview") : i18n("Preview")
                        icon.name: root.previewing ? "media-playback-stop" : "media-playback-start"
                        enabled: picker.currentIndex >= 0 && !root.reading
                        onClicked: {
                            if (root.previewing) {
                                root.callReader("Stop", []);
                            } else {
                                root.callReader("PreviewVoice", [popup.voice]);
                            }
                        }
                    }

                    PlasmaComponents.Button {
                        Layout.fillWidth: true
                        text: i18n("Use voice")
                        enabled: picker.currentIndex >= 0 && popup.voice !== root.savedVoice && !root.previewing
                        onClicked: root.callReader("SetVoice", [popup.voice], () => {
                            popup.candidate = "";
                        })
                    }
                }

                RowLayout {
                    Layout.topMargin: Kirigami.Units.largeSpacing

                    PlasmaComponents.Label {
                        Layout.fillWidth: true
                        text: i18n("Speech rate")
                    }

                    PlasmaComponents.Label {
                        text: i18n("%1×", speed.value.toFixed(2))
                    }
                }

                PlasmaComponents.Slider {
                    id: speed

                    Layout.fillWidth: true
                    enabled: root.ready
                    from: 0.5
                    to: 2.0
                    stepSize: 0.05
                    value: root.rate
                    Accessible.name: i18n("Speech rate")
                    onMoved: rateDelay.restart()

                    Timer {
                        id: rateDelay
                        interval: 120
                        onTriggered: root.callReader("SetRate", [speed.value])
                    }
                }

                RowLayout {
                    Layout.topMargin: Kirigami.Units.largeSpacing

                    PlasmaComponents.Label {
                        Layout.fillWidth: true
                        text: root.statusText
                        wrapMode: Text.Wrap
                    }

                    PlasmaComponents.Button {
                        text: i18n("Stop reading")
                        icon.name: "media-playback-stop"
                        enabled: root.reading
                        onClicked: root.callReader("Stop", [])
                    }
                }

                PlasmaComponents.Label {
                    Layout.fillWidth: true
                    visible: text.length > 0
                    text: popup.errorText
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    color: Kirigami.Theme.negativeTextColor
                }
            }
        }
    }
}
