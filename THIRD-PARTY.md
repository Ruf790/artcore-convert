# Third-party software in ArtCore Convert

ArtCore Convert is free software under the GNU GPL v3 (see LICENSE). It ships with or uses:

| Component | License | Where |
|---|---|---|
| **FFmpeg** (BtbN/FFmpeg-Builds, GPL build: x264, x265, SVT-AV1, NVENC headers, AMF, oneVPL/QSV, Opus, LAME …) | GPL v3 (the build as a whole) | https://ffmpeg.org · https://github.com/BtbN/FFmpeg-Builds |
| **Qt 6 / PySide6** | LGPL v3 | https://www.qt.io · https://wiki.qt.io/Qt_for_Python |
| **QtAwesome** + Font Awesome 6 Free, Material Design Icons | MIT; icons CC BY 4.0 / SIL OFL 1.1 / Apache 2.0 | https://github.com/spyder-ide/qtawesome |
| **psutil** | BSD 3-Clause | https://github.com/giampaolo/psutil |
| **Python** | PSF License | https://www.python.org |

The FFmpeg source code for the bundled build is available from the links above (the exact build is named in
`ffmpeg/VERSION.txt`); FFmpeg's own license text is in `ffmpeg/LICENSE.txt`.

NVIDIA NVENC, AMD AMF and Intel Quick Sync are used through the graphics driver installed on your PC; no
part of them is shipped with this program.
