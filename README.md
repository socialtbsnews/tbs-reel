# TBS Reel (desktop app)

Makes reels in the TBS frame from photos and video clips, as a 30 fps MP4 in two sizes:
**9:16 reel (1080 x 1920)** or **4:5 post (1080 x 1350)** (pick under SIZE).

**Download:** [Windows](https://github.com/socialtbsnews/tbs-reel/releases/latest/download/TBS-Reel-Windows.exe) ·
[Mac](https://github.com/socialtbsnews/tbs-reel/releases/latest/download/TBS-Reel-Mac.zip)

## How to use
1. **Add clips:** drop photos and video clips onto the window (or click **+ Add photos / videos**).
   Everything you add is also kept in the **MEDIA** box (bottom left), so you can use it again:
   double-click a picture there, or drag it onto the timeline. Dropping files on the MEDIA box only
   stores them. Dropping a music file sets the music; dropping font files adds them.
2. **Edit on the timeline** (bottom):
   - Click anywhere to move the playhead (yellow line). **▶ Play** plays the whole reel from there,
     with its sound (clip sound, music, mutes and bleeps, as in the made reel).
   - Drag a clip to move it. Drag a clip's edge to trim it. Right-click a clip for more.
   - **✂ Split**, **Trim start**, **Trim end** and **✕ Delete** work at the playhead. **↶ Undo** (Ctrl+Z).
   - **Mute** takes the sound off one clip. Untick "Use the sound of the video clips" to take it off all.
   - **Bleep:** put the playhead where the word starts and click **Bleep from here**, then where it ends
     and click **End bleep here** (works while playing too). The word's sound becomes a beep (red mark).
     Click **Remove bleep** with the playhead inside a bleep to remove it.
   - **Timeline − Fit +** zooms the timeline. Keys: Space play, S split, B bleep, M mute, Delete,
     ← → one frame, Shift+← → one second.
3. **On the preview:**
   - Drag the picture to pick the subject, and zoom with the mouse wheel or the **Zoom** slider
     (e.g. turn a wide 1920 x 1080 video into a 9:16 reel on one person). **Reset picture** undoes it.
   - **▢ Blur box**, then drag a box over a face or anything sensitive. Drag the box to move it, its corner
     to resize it, ✕ to remove it. Right-click it to make it start or end at the playhead (blue bar on the
     timeline), or to **follow something that moves**: move the playhead, drag the box onto the subject
     again, repeat every second or so; the box glides between the places you set.
   - Drag the **headline** to move it. **Reset place** puts it back.
4. **Type the text** (left side): **Date**, **Credit**, and the **Headline** (select words and click
   **Bold** or **Yellow**; **Align** left, centre or right). **Dark shade** size and darkness can be changed.
5. Right side: framing, photo zoom, music, **Fade to black** (start, end, between clips),
   **Disclaimer** card (three TBS disclaimers, at the start or end), TBS logo and end card.
6. **Make reel** and choose where to save.

## Fonts
The app looks for fonts installed on the computer and picks TBS's fonts first:
**FF Meta Serif Pro** (headline) and **November Condensed LCG** (date and credit). The typing boxes show the chosen fonts too. Fonts can also be added inside the app
with **Add fonts…** (they are copied to `~/.tbs_reel_fonts` and stay available).
Paid fonts are not stored in this repository.

Built-in free fonts (SIL Open Font License, see `fonts/`): Noto Sans Bengali, Hind Siliguri,
Noto Serif (with italics), Roboto Condensed. The disclaimer cards use the headline font's italic
file when the computer has one (e.g. FF Meta Serif Pro Italic). A font with only a bold file gets a slightly thicker look when Bold is used.
Letters are balanced to look the same size whatever fonts are picked.

## Quality
H.264 High profile, CRF 16 (very high quality), AAC 256 kbps, fast-start MP4.
Pictures are scaled once, from the originals.

## Build the app
- **Windows:** install Python 3.12 from python.org (tick "Add python.exe to PATH"), then double-click
  `build_windows.bat`. The app is `dist\TBS Reel.exe`.
- **Mac:** install Python 3.12 from python.org, then run `./build_mac.sh` in Terminal.
  The app is `dist/TBS Reel.app`.
- Every push to `main` builds both on GitHub and publishes them to the latest release.
