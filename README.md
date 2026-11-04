# TBS Reel (desktop app)

Makes reels in the TBS reel frame: 1080 x 1350 (4:5), 30 fps MP4, from photos and video clips.

**Download:** [Windows](https://github.com/socialtbsnews/tbs-reel/releases/latest/download/TBS-Reel-Windows.exe) ·
[Mac](https://github.com/socialtbsnews/tbs-reel/releases/latest/download/TBS-Reel-Mac.zip)

## How to use
1. **Add clips:** drop photos and video clips onto the window (or click **+ Add photos / videos**).
   Dropping a music file sets the background music; dropping font files adds them.
2. **Edit on the timeline** (bottom of the window): click a clip to select it.
   - **▶ Play** plays it in the preview; the slider (or clicking inside the selected clip) picks a moment.
   - **✂ Split here** cuts the clip in two at that moment; **Cut start here / Cut end here** trims it.
   - **✕ Delete clip** removes the selected clip. **↶ Undo** brings back the last change (Ctrl+Z).
   - Drag a clip along the timeline (or use **◀ Move / Move ▶**) to change the order.
   - Photos: set how many seconds each stays on screen.
   - Keys: Space = play/pause, S = split, Delete = delete clip, ← → = one frame.
3. **Type the text** (left side):
   - **Date** (starts as today; **Today** resets it) and **Credit**, e.g. `Video: Shariar Gaalib`.
   - **Headline**: select words and click **Bold** or **Yellow**
     (click again to take it off; **Plain** clears both). Enter starts a new line; long headlines
     wrap and shrink a little if they need more than 3 lines.
   - **Fonts:** pick the headline font and the date/credit font, and the headline size.
4. **Make reel** and choose where to save.

## The frame
- TBS logo top left; date and credit top right.
- Dark panel at the bottom with the headline, and the teal strip along the bottom edge.
- **Framing:** Auto puts wide (landscape) videos full width at the top and lets tall ones fill the
  frame. You can also force "Fill the frame", "Full width at the top" or "Whole picture in the middle".
- **Photos:** gentle zoom (alternating in and out) or still. Default 3 seconds each.
- **Sound:** clips keep their own sound (can be turned off). Optional music repeats if it is shorter
  than the reel and fades out at the end.
- Optional **TBS end card** (off by default).

## Fonts
The app looks for fonts installed on the computer and picks TBS's fonts first:
**FF Meta Serif Pro** (headline) and **November Condensed LCG** (date and credit). The typing boxes show the chosen fonts too. Fonts can also be added inside the app
with **Add fonts…** (they are copied to `~/.tbs_reel_fonts` and stay available).
Paid fonts are not stored in this repository.

Built-in free fonts (SIL Open Font License, see `fonts/`): Noto Sans Bengali, Hind Siliguri,
Noto Serif, Roboto Condensed. A font with only a bold file gets a slightly thicker look when Bold is used.
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
