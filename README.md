# Plasma Read Aloud

Select to Speak reads on demand in Plasma 6 on Wayland. It runs in your user session and uses an offline Piper voice. A click-through amber highlight follows the sentence being spoken.

Point at text, a control, a menu item, a desktop panel, or a pop-up and press **Alt+Esc**. The reader speaks the accessible text or name at that spot, including names for icons in the Plasma system tray and task manager. While pointing inside the active window, a text selection takes priority. Pointing inside a focused editor starts at its caret when no text is selected. Press **Alt+Esc** again or tap and release **Ctrl** by itself to stop. Ctrl combinations such as **Ctrl+C** still go to the app.

Click the headphones icon in the Plasma system tray to change speech rate or stop playback. The default is **1.20×**. The slider moves in **0.05×** steps between **0.50×** and **2.00×**. Changes are saved automatically and take effect at the next sentence.

On a web page, pointing at ordinary text reads from that word to the end of the article or the enclosing reading region. Pointing at a link starts with its full label and continues through its enclosing paragraph, list item, or similar container. Reading scrolls the text into view when the app supports accessibility scrolling. In an editor, caret reading begins at the exact insertion point.

Shortcut assignments live under **System Settings → Keyboard → Shortcuts → KWin**. [KWin scripting API](https://develop.kde.org/docs/plasma/kwin/api/), [Apple Read & Speak](https://support.apple.com/en-gb/guide/mac-help/mh27448/27/mac/27)

## Install

On Fedora KDE, install the desktop dependencies if they are missing.

```sh
sudo dnf install python3-gobject python3-cairo gtk4 gtk4-layer-shell at-spi2-core pulseaudio-utils espeak-ng
```

Run the installer from a Plasma Wayland session. Python 3.14 is used only for the optional neural voice environment. The desktop service runs with the system Python. The natural voice download is about 61 MB.

```sh
./install --with-piper python3.14
```

Running `./install` uses an existing Piper environment if present, with eSpeak NG available as a fallback. The installer enables desktop accessibility, loads the KWin script, and starts the user service. An app opened before accessibility was enabled may need to be reopened.

Chromium-based browsers publish page accessibility on demand. The reader requests the native accessibility tree when you invoke it. [Chromium accessibility overview](https://chromium.googlesource.com/chromium/src/+/main/docs/accessibility/overview.md)

If an app exposes no readable accessibility data at the pointer, the reader can use OCR on the visible window, menu, or pop-up under it. Install Spectacle, Tesseract, and a Tesseract language pack, then point at or near visible text. Selection, offscreen text, and names for icons without visible labels depend on the app’s accessibility data. Screenshots used for OCR are temporary.

```sh
sudo dnf install spectacle tesseract tesseract-langpack-eng
```

## Settings and checks

Set variables in `~/.config/select-to-speak/env` and restart the service with `systemctl --user restart select-to-speak.service`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SELECT_TO_SPEAK_RATE` | `1.20` | Initial speech rate until a rate is saved from the tray. |
| `SELECT_TO_SPEAK_VOICE` | Lessac medium | Path to a Piper ONNX voice with its matching JSON file. |
| `SELECT_TO_SPEAK_PIPER` | Installed Piper Python | Python executable with the Piper package installed. |
| `SELECT_TO_SPEAK_OCR` | `1` | Set to `0` to disable image recognition. |
| `SELECT_TO_SPEAK_OCR_LANGUAGE` | `eng` | Tesseract language code. |

```sh
systemctl --user status select-to-speak.service
journalctl --user -u select-to-speak.service -n 30
/usr/bin/python3 -m unittest discover -s tests -v
```
