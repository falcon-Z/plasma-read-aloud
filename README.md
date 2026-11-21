# Plasma Read Aloud

Select to Speak reads on demand in Plasma 6.4 or later on Wayland. It runs in your user session and uses offline Piper voices. A click-through amber highlight follows the sentence being spoken.

Point at text, a control, a menu item, or a pop-up and press **Ctrl+Esc**. A short rising tone confirms the reader received the shortcut. If text lookup fails, the reader says “No readable text found at the pointer.” A lower, falling tone provides feedback if the status message itself cannot play. The tray tooltip also shows the current status.

The reader checks for selected text in the active window first, even if the pointer has moved away. With no selection, it speaks the accessible text or name at the pointer, including names for icons in the Plasma system tray and task manager. Pointing inside a focused editor starts at its caret when no text is selected. Press **Ctrl+Esc** again or press **Esc** to stop at any stage. While the reader is idle, Esc goes to the app.

Click the speech bubble icon in the Plasma system tray to open the Select to Speak widget. Pick a voice and click **Preview** to hear a sample at the current speech rate. Click **Use voice** to save your choice for the next reading. The widget also lets you stop playback or adjust the speech rate from **0.50×** to **2.00×** in **0.05×** steps. The default is **1.20×**, and rate changes take effect at the next sentence.

On a web page, pointing at ordinary text reads from that word to the end of the article or the enclosing reading region. Prose reading skips nearby playback controls and input fields. Point directly at a control to hear its label. Pointing at a link starts with its full label and continues through its enclosing paragraph, list item, or similar container. Pointer reading can follow the text by scrolling the active window when the app supports accessibility scrolling. In an editor, caret reading begins at the exact insertion point and leaves the viewport in your control.

Change **Select to Speak: Read aloud** under **System Settings → Keyboard → Shortcuts → KWin**. [KWin scripting API](https://develop.kde.org/docs/plasma/kwin/api/), [Apple Read & Speak](https://support.apple.com/en-gb/guide/mac-help/mh27448/27/mac/27)

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

If an app exposes no readable accessibility data at the pointer, the reader can use OCR on the visible window, menu, or pop-up under it. Install Tesseract and a language pack, then point at or near visible text. OCR reads from that word through nearby lines in the same column, including across breaks in Tesseract's layout. The highlight uses the positions in the captured image. The reader captures directly through KWin where available and uses a background Spectacle capture otherwise. Selection, offscreen text, and names for icons without visible labels depend on the app’s accessibility data. Screenshots used for OCR are temporary.

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
| `SELECT_TO_SPEAK_OCR` | `1` | Set to `0` to disable image recognition. |
| `SELECT_TO_SPEAK_OCR_LANGUAGE` | `eng` | Tesseract language code. |

```sh
systemctl --user status select-to-speak.service
journalctl --user -u select-to-speak.service -n 30
/usr/bin/python3 -m unittest discover -s tests -v
```
