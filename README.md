# Plasma Read Aloud

Select to Speak reads aloud on demand alongside Plasma's Zoom effect. It runs in Plasma 6.4 or later on Wayland and uses offline Piper voices. A click-through highlight marks the text being spoken.

| Default shortcut | Action |
| --- | --- |
| **Ctrl+Alt+R** | Read under the pointer. Invoke it again to read from a new spot. |
| **Ctrl+Alt+Enter** | Read the focused control, text field, or document. |
| **Ctrl+Esc** | Read the active selection, or read under the pointer if there is no selection. Press it again to stop. |
| **Ctrl+Alt+Space** | Pause or resume from the start of the current sentence. |
| **Ctrl+Alt+H / K** | Go to the previous or next sentence. |
| **Tap Ctrl** | Stop reading, including while looking up text, paused, or previewing a voice. The shortcut fires when you release Ctrl by itself. |

The pointer command reads where you point, even with text selected elsewhere. Point at a word to start reading there, or at a control, menu item, pop-up, or tray icon to hear its label. On web pages, reading can continue through the article or enclosing reading region. At a link it starts with the full link label. In a focused editor, **Ctrl+Alt+Enter** reads the selection or starts at the caret. At the end of the editor it reads the last nonempty line. Protected fields are skipped.

Reading keeps the app's scroll position under your control by default. If you want pointer reading to bring offscreen sentences into view, enable auto-scroll below.

Open the speech bubble in the Plasma system tray for playback controls, reading status, voices, and speech rate. **Preview** plays a sample at the current rate. **Use voice** saves your choice for the next reading. The default rate is **1.20×**, and rate changes take effect at the next sentence. A short rising tone confirms a read request. When the target is unavailable, the reader gives spoken feedback and shows the reason in the tray.

Customize the **Select to Speak** actions under **System Settings → Keyboard → Shortcuts → KWin**. [Apple Read & Speak](https://support.apple.com/en-gb/guide/mac-help/mh27448/27/mac/27), [Windows Magnifier reading](https://support.microsoft.com/en-us/accessibility/windows/magnifier/how-to-use-magnifier-reading)

## Install

On Fedora KDE, install the desktop dependencies if they are missing.

```sh
sudo dnf install python3-gobject python3-cairo gtk4 gtk4-layer-shell at-spi2-core pulseaudio-utils espeak-ng
```

Run the installer from a Plasma Wayland session. Python 3.14 is used only for the optional neural voice environment. The desktop service runs with the system Python. The installer downloads three Piper voices, **Lessac** and **Ryan** in US English and **Alba** in UK English, for about 181 MiB total.

```sh
./install --with-piper python3.14
```

Running `./install` uses any existing Piper environment and downloaded voices. eSpeak NG is available as a fallback. The installer adds the widget to the Plasma system tray, enables desktop accessibility, loads the KWin script, and starts the user service. If the icon is hidden, set **Select to Speak** to **Always shown** under **Configure System Tray → Entries**. An app opened before accessibility was enabled may need to be reopened.

To add more [Piper voices](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/VOICES.md), put each `.onnx` file and its matching `.onnx.json` file in `${XDG_DATA_HOME:-$HOME/.local/share}/select-to-speak/voices`, then restart `select-to-speak.service`. The preview uses an English sample.

Chromium-based browsers and Electron apps such as ChatGPT desktop can publish page accessibility on demand. The reader requests the native accessibility tree when you invoke it. If ChatGPT desktop was already running when accessibility was enabled, fully quit and reopen it once so its new process can register. [Chromium accessibility overview](https://chromium.googlesource.com/chromium/src/+/main/docs/accessibility/overview.md)

If an app exposes no readable accessibility data at the pointer, the reader can use OCR on the visible window, menu, or pop-up under it. Install Tesseract and a language pack, then point at or near visible text. OCR reads from that word through nearby lines in the same column, and the highlight uses the positions in the captured image. The reader captures directly through KWin where available and uses a background Spectacle capture otherwise. Keyboard focus, selection, offscreen text, and names for icons without visible labels depend on the app’s accessibility data. Screenshots used for OCR are temporary.

```sh
sudo dnf install spectacle tesseract tesseract-langpack-eng
```

## Settings and checks

Set variables in `~/.config/select-to-speak/env` and restart the service with `systemctl --user restart select-to-speak.service`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SELECT_TO_SPEAK_RATE` | `1.20` | Initial speech rate until a rate is saved from the widget. |
| `SELECT_TO_SPEAK_VOICE` | Lessac medium | Path to a Piper ONNX voice with its matching JSON file. Sets the initial choice until a voice is saved from the widget. |
| `SELECT_TO_SPEAK_PIPER` | Installed Piper Python | Python executable with the Piper package installed. |
| `SELECT_TO_SPEAK_AUDIO_CUES` | `1` | Set to `0` to turn off the shortcut feedback sounds. |
| `SELECT_TO_SPEAK_AUTO_SCROLL` | `0` | Set to `1` to scroll to offscreen sentences during pointer reading in the active window, when the app supports accessibility scrolling. |
| `SELECT_TO_SPEAK_OCR` | `1` | Set to `0` to disable image recognition. |
| `SELECT_TO_SPEAK_OCR_LANGUAGE` | `eng` | Tesseract language code. |

```sh
systemctl --user status select-to-speak.service
journalctl --user -u select-to-speak.service -n 30
/usr/bin/python3 -m unittest discover -s tests -v
```
