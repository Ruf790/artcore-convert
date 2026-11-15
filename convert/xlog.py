"""1.6: the conversion log in sections — the source and its streams, what the result is to be
(video / audio / subtitles, the filters, the encoder and its settings), each ffmpeg run with its output, the
statistics of the streams and the end: completed / failed / stopped, the time, the memory, the sizes."""
from __future__ import annotations

import os
import re
import time

from . import VERSION
from . import encoders as E
from . import probe as P

LINE = "-" * 60

_RE_OUT = re.compile(r"Output stream #0:(\d+) \((\w+)\): (?:(\d+) frames encoded(?: \((\d+) samples\))?; )?"
                     r"(\d+) packets muxed \((\d+) bytes\)")
_RE_IN = re.compile(r"Input stream #(\d+):(\d+) \((\w+)\): (\d+) packets read \((\d+) bytes\);")
_RE_DEC = re.compile(r"(\d+) frames decoded")
_RE_ERRS = re.compile(r"(\d+) decode errors")
_RE_SAMPLES = re.compile(r"\((\d+) samples\)")
# the verbose lines of ffmpeg that say nothing about the result: left out of the log
NOISE = re.compile(r"Decoder thread received EOF|Decoder returned EOF|Terminating (?:decoder|demuxer|muxer) thread|"
                   r"\[AVIOContext|All streams finished|No more output streams to write|Reinit context to|"
                   r"Last message repeated|EOF while reading input|Adding streams from explicit maps|"
                   r"Created (?:video|audio|subtitle) stream from|Creating output stream from|^\[\w+#\d+:\d+/\w+ @ 0x[0-9a-f]+\]\s*$|"
                   r"Starting thread|Filtergraph|Total: \d+ packets")
_RE_SIZES = re.compile(r"video:\s*([\d.]+)\s*k(?:i)?B\s+audio:\s*([\d.]+)\s*k(?:i)?B\s+subtitle:\s*([\d.]+)\s*k(?:i)?B"
                       r"\s+other streams:\s*([\d.]+)\s*k(?:i)?B\s+global headers:\s*([\d.]+)\s*k(?:i)?B\s+"
                       r"muxing overhead:\s*([\d.]+|unknown)%?", re.I)
_RE_FRAMES = re.compile(r"frame\s+([IPB]):\s*(\d+),?\s+Avg QP:\s*([\d.]+)\s+(?:kb/s:\s*([\d.]+)|size:\s*(\d+))?", re.I)
_RE_PROGRESS = re.compile(r"^(frame=|size=)")
_RE_OPTIONS = re.compile(rb"(x26[45][^\x00]{0,120}?)options: ([\x20-\x7e]{20,4000})")

PHASE_TITLE = {"main": "MAIN PASS", "pass1": "PASS 1 (ANALYSIS)", "vidstab": "STABILIZATION (ANALYSIS)",
               "metrics": "QUALITY METRICS", "split": "SPLITTING", "pre": "PRE PASS", "post": "POST PASS"}


def stamp(when: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when)) + f".{int(when * 1000) % 1000:03d}"


def dur(sec: float) -> str:
    """«1h 50m 30s 656ms»."""
    sec = max(0.0, float(sec or 0))
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return (f"{h}h " if h else "") + (f"{m}m " if h or m else "") + f"{s}s {ms}ms"


def size_text(n: float) -> str:
    """«47171239 bytes (44.99 MiB / 47.17 MB)»."""
    n = int(n or 0)
    if n >= 1 << 30:
        return f"{n} bytes ({n / (1 << 30):.2f} GiB / {n / 1e9:.2f} GB)"
    return f"{n} bytes ({n / (1 << 20):.2f} MiB / {n / 1e6:.2f} MB)"


def _ratio(w: int, h: int, sar: str = "") -> str:
    if not w or not h:
        return "?"
    r = w / h
    try:
        if sar and sar not in ("0:1", "1:1"):
            a, b = sar.split(":")
            r *= int(a) / int(b)
    except (ValueError, ZeroDivisionError):
        pass
    for name, val in (("16:9", 16 / 9), ("4:3", 4 / 3), ("21:9", 64 / 27), ("2.35:1", 2.35), ("2.39:1", 2.39),
                      ("1.85:1", 1.85), ("1:1", 1.0), ("5:4", 1.25), ("9:16", 9 / 16)):
        if abs(r - val) < 0.012:
            return f"{name} ({r:.3f})"
    return f"{r:.3f}"


def _fps(f: float) -> str:
    return f"{f:.3f}" if f else "?"


def stream_line(s, external: str = "") -> str:
    """A source stream as one line: «H264 (High) 1920x1080p 16:9 YUV420P 10bit 23.976 HDR10»."""
    if s.kind == "video":
        bits = [P.codec_name(s).upper() + (f" ({s.profile})" if s.profile else ""),
                f"{s.width}x{s.height}{'i' if s.interlaced else 'p'}", _ratio(s.width, s.height, s.sar).split(" ")[0],
                (s.pix_fmt or "?").upper()]
        if s.bits and s.bits != 8:
            bits.append(f"{s.bits}bit")
        bits.append(_fps(s.fps))
        if s.hdr:
            bits.append(s.hdr)
        head = "Video:"
    elif s.kind == "audio":
        bits = [P.codec_name(s).upper(), f"{s.channels}ch" + (f" ({s.layout})" if s.layout else ""),
                f"{s.rate / 1000:g}kHz" if s.rate else "?kHz"]
        if s.bitrate:
            bits.append(f"{s.bitrate // 1000}kbps")
        head = "Audio:"
    else:
        bits = [P.codec_name(s).upper() if s.codec else "?"]
        head = "Subtitle:"
    if s.lang_label():
        bits.append(s.lang_label())
    if s.title:
        bits.append(f'"{s.title}"')
    if s.default:
        bits.append("(Default)")
    if s.forced:
        bits.append("(Forced)")
    if external:
        bits.append(f"(External: {external})")
    return f"{head:<10}#{s.index} " + " ".join(bits)


def file_lines(path: str, info) -> list[str]:
    out = [f"Input file: {path}"]
    if info is None:
        out.append("File:      (not read)")
        return out
    if info.error:
        out.append(f"File:      error: {info.error}")
        return out
    frames = ""
    if info.video is not None and info.video.fps and info.duration:
        frames = f" ({int(round(info.video.fps * info.duration))} frames)"
    rate = f" {info.bitrate // 1000}kbps" if info.bitrate else ""
    out.append(f"File:      {(info.format or '?').upper()} {P.fmt_size(info.size)} {dur(info.duration)}{frames}{rate}")
    for s in info.streams:
        if s.kind in ("video", "audio", "subtitle") and not s.cover:
            out.append(stream_line(s))
    return out


def encoder_options(path: str) -> list[str]:
    """The settings x264 / x265 wrote into the stream («options: cabac=1 ref=3 …») as «cabac = 1» lines."""
    try:
        with open(path, "rb") as f:
            head = f.read(4 << 20)
    except OSError:
        return []
    m = _RE_OPTIONS.search(head)
    if not m:
        return []
    name = m.group(1).decode("ascii", "replace").strip(" -")
    out = [f"Encoder: {name}"] if name else []
    for part in m.group(2).decode("ascii", "replace").split():
        if "=" in part:
            k, v = part.split("=", 1)
            out.append(f"{k} = {v}")
        elif part.startswith("no-"):
            out.append(f"{part[3:]} = 0")
        else:
            out.append(f"{part} = 1")
    return out


def _arg_after(cmd: list[str], flag: str) -> str:
    try:
        return cmd[cmd.index(flag) + 1]
    except (ValueError, IndexError):
        return ""


def _split_cmd(line: str) -> list[str]:
    import shlex
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        return line.split()


def phases(lines: list[str], kinds: list[tuple[str, float]]) -> list[dict]:
    """The log cut at its «$ command» lines: [{kind, started, cmd, lines}]."""
    out: list[dict] = []
    n = 0
    for ln in lines:
        if ln.startswith("$ "):
            kind, when = kinds[n] if n < len(kinds) else ("main", 0.0)
            n += 1
            out.append({"kind": kind, "started": when, "cmd": ln[2:], "lines": []})
        elif out:
            out[-1]["lines"].append(ln)
        else:
            out.append({"kind": "main", "started": 0.0, "cmd": "", "lines": [ln]})
    return out


def stats(lines: list[str]) -> list[str]:
    """What ffmpeg (-loglevel verbose) and the encoder said at the end, as statistics."""
    out: list[str] = []
    frames = []
    for ln in lines:
        m = _RE_FRAMES.search(ln)
        if m and ("libx26" in ln or "x26" in ln.lower()):
            kbps = f", bitrate = {m.group(4)} kbps" if m.group(4) else ""
            size = f", avg size = {m.group(5)} bytes" if m.group(5) else ""
            frames.append(f"{m.group(1).upper()} frames: {m.group(2)}, avg QP = {m.group(3)}{size}{kbps}")
    if frames:
        out.append("Frame statistics:")
        out += [" " + f for f in frames]
    for ln in lines:
        m = _RE_IN.search(ln)
        if not m:
            continue
        d, e, smp = _RE_DEC.search(ln), _RE_ERRS.search(ln), _RE_SAMPLES.search(ln)
        dec = f", Decoded frames: {d.group(1)}" if d else ""
        dec += f" ({smp.group(1)} samples)" if smp else ""
        errs = f", Decode errors: {e.group(1)}" if e and e.group(1) != "0" else ""
        out.append(f"Input {m.group(3)} stream #{m.group(1)}:{m.group(2)}: Read packets: {m.group(4)} "
                   f"({m.group(5)} bytes){dec}{errs}")
    for m in (x for x in (_RE_OUT.search(ln) for ln in lines) if x):
        enc = f"Encoded frames: {m.group(3)}, " if m.group(3) else "Copied, "
        out.append(f"Completed {m.group(2)} stream at index: {m.group(1)}: {enc}Written packets: {m.group(5)} "
                   f"({m.group(6)} bytes)")
    last = [ln for ln in lines if _RE_PROGRESS.match(ln.strip())]
    if last:
        out.append("Last position: " + re.sub(r"\s+", " ", last[-1].strip()))
    return out


def sizes(lines: list[str]) -> dict:
    mine = [ln for ln in lines if "[out#0" in ln] or lines        # the result, not the preview frame
    for ln in reversed(mine):
        m = _RE_SIZES.search(ln)
        if m:
            return {"video": float(m.group(1)), "audio": float(m.group(2)), "subtitle": float(m.group(3)),
                    "other": float(m.group(4)), "headers": float(m.group(5)), "overhead": m.group(6)}
    return {}


def _codec_label(codec: str) -> str:
    if codec == "copy":
        return "COPY"
    a = E.A_BY_ID.get(codec)
    return (a.label.split(" (")[0] if a else codec).upper()


def build(j, J, lines: list[str], kinds: list[tuple[str, float]], *, started: float, ended: float,
          profile: str, peak_mem: int = 0, out_path: str = "", tr=None) -> str:
    """The whole log of one task. J: the job module (passed in: no import cycle)."""
    st = j.settings
    c = E.container(st.get("container", "mkv"))
    L: list[str] = [LINE, f"ArtCore Convert {VERSION}", LINE, f"ENCODING STARTED {stamp(started)}", LINE]
    # ── the source(s)
    L += file_lines(j.src, j.info)
    for p in j.joins:
        L.append("")
        L += [ln.replace("Input file:", "Joined file:", 1) for ln in file_lines(p, J.file_info(j, p))]
    for n, e in enumerate(j.ext):
        info = J.file_info(j, e["path"])
        kind = e.get("kind", "audio")
        s = next(iter(info.of(kind)), None) if info is not None and not info.error else None
        if s is not None:
            line = stream_line(s, os.path.basename(e["path"]))
            line = line.replace(f"#{s.index} ", f"#ext{n + 1} ", 1)
            if e.get("delay"):
                line += f" delay {e['delay']}ms"
        else:
            line = f"{'Audio:' if kind == 'audio' else 'Subtitle:':<10}(External: {os.path.basename(e['path'])}) not read"
        L.append(line)
    L.append(LINE)
    # ── the result
    out = j.out or out_path or ""
    L.append(f"Output file:   {os.path.basename(out) if out else '—'}")
    L.append(f"Output folder: {os.path.dirname(out) if out else '—'}")
    L.append(f"Output format: {c.label}")
    L.append(f"Profile:       {profile}")
    segs = J.segments(j)
    if segs:
        L.append(f"Trim:          {len(segs)} piece(s), result {dur(J.out_duration(j))}")
        for a, b in segs:
            L.append(f"               {J.fmt_tc(a)} - {J.fmt_tc(b)}")
    sp = st.get("split") or {}
    if sp.get("mode") in ("size", "time"):
        L.append(f"Split:         by {'size: %s MB' % sp.get('size_mb') if sp['mode'] == 'size' else 'time: %s min' % sp.get('minutes')}")
    L.append(LINE)
    try:
        pl = J.plan(j)
    except Exception:                                       # noqa: BLE001 — a log must not fail
        pl = {"video": [], "audio": [], "subs": [], "warnings": []}
    main = next((p for p in phases(lines, kinds) if p["kind"] == "main"), None)
    cmd = _split_cmd(main["cmd"]) if main and main["cmd"] else []
    v = st.get("video", {})
    if pl["video"]:
        s = pl["video"][0]
        L.append("Video general:")
        enc = E.BY_ID.get(j.enc_used) if j.enc_used and j.enc_used != "copy" else None
        try:
            ow, oh = J.out_size(j, enc) if enc is not None else (s.width, s.height)
        except Exception:                                   # noqa: BLE001
            ow, oh = s.width, s.height
        L.append(f" Resolution:  {s.width}x{s.height} > {ow}x{oh}")
        L.append(f" Aspect:      {_ratio(s.width, s.height, s.sar)} > {_ratio(ow, oh)}")
        L.append(f" Interlace:   {'Interlaced' if s.interlaced else 'Progressive'}")
        try:
            ofps = J.out_fps(j) or s.fps
        except Exception:                                   # noqa: BLE001
            ofps = s.fps
        L.append(f" Framerate:   {_fps(s.fps)} > {_fps(ofps)} {'CFR' if v.get('fps') else ''}")
        if s.hdr:
            L.append(f" HDR:         {s.hdr}")
        flt = _arg_after(cmd, "-vf") or _arg_after(cmd, "-filter_complex")
        # the picture's chain only: not the preview frame of the Status tab, not the sound
        flt = ";".join(x for x in flt.split(";") if "vprev" not in x and "[vpv]" not in x and "[ao" not in x
                       and not x.startswith("[vfull]split"))
        if flt:
            L.append(f" Filters:     {flt}")
        if v.get("mode") == "copy" or j.enc_used == "copy":
            L.append(f" Codec:       {P.codec_name(s).upper()} > COPY")
        else:
            name = enc.ffname.upper() if enc else (_arg_after(cmd, "-c:v") or "?").upper()
            rc = v.get("rc", "quality")
            if rc == "quality" or (enc is not None and not enc.rc):
                q = v.get("quality") or (enc.q_default if enc else "")
                how = ("CQ " if enc is not None and enc.hw else "CRF ") + str(q)
            elif rc == "bitrate":
                how = f"{v.get('bitrate')} kbps"
            else:
                how = f"size {v.get('size_mb')} MB"
            if int(v.get("passes", 1) or 1) == 2 and rc in ("bitrate", "size"):
                how += " 2-pass"
            thr = _arg_after(cmd, "-threads")
            pix = _arg_after(cmd, "-pix_fmt")
            L.append(f" Codec:       {P.codec_name(s).upper()} > {name} {how}" +
                     (f" with {thr} threads" if thr and thr != "0" else "") + (f", {pix.upper()}" if pix else ""))
            if enc is not None:
                L.append(f" Encoder:     {enc.ffname} ({E.VENDOR_LABEL.get(enc.vendor, '') if enc.hw else 'CPU'})")
            try:
                i = cmd.index("-c:v")
                tail = cmd[i:]
                if "-f" in tail:                            # the result ends there (then the preview frame)
                    tail = tail[:tail.index("-f")]
                opts = []
                k = 0
                while k < len(tail):
                    a = tail[k]
                    if a in ("-sn", "-dn", "-an"):
                        k += 1
                        continue
                    if a.startswith(("-c:a", "-c:s", "-b:a", "-ac", "-ar", "-af", "-filter:a", "-map", "-metadata",
                                     "-disposition", "-max_muxing", "-passlogfile")):
                        k += 2
                        continue
                    if a.startswith("-"):
                        val = tail[k + 1] if k + 1 < len(tail) and not tail[k + 1].startswith("-") else ""
                        opts.append(f"{a.lstrip('-')} = {val}" if val else a.lstrip("-"))
                        k += 2 if val else 1
                    else:
                        k += 1
                if opts:
                    L.append(" Encoder arguments:")
                    L += ["  " + o for o in opts[:60]]
            except ValueError:
                pass
        L.append(LINE)
    if pl["audio"]:
        L.append("Audio general:")
        for an, (s, codec) in enumerate(pl["audio"]):
            no = J.ext_no(s)
            src = f"#ext{no + 1}" if no is not None else f"#{s.index}"
            trk = j.track(s) if no is None else (j.ext[no] if no < len(j.ext) else {})
            kb = _arg_after(cmd, f"-b:a:{an}").rstrip("k") or (trk.get("kbps") if isinstance(trk, dict) else 0)
            ach, arate = _arg_after(cmd, f"-ac:a:{an}"), _arg_after(cmd, f"-ar:a:{an}")
            delay = (trk.get("delay") if isinstance(trk, dict) else 0) or 0
            bits = [f" Stream {src}: {P.codec_name(s).upper()} {s.channels}ch {s.rate / 1000:g}kHz > {_codec_label(codec)}"]
            if codec != "copy" and kb:
                bits.append(f"{kb} kbps")
            if codec != "copy" and ach:
                bits.append(f"{ach}ch")
            if codec != "copy" and arate:
                bits.append(f"{int(arate) / 1000:g}kHz")
            if delay:
                bits.append(f"delay {delay} ms")
            lang = (j.smeta(s).get("lang") if no is None else (j.ext[no].get("lang") if no < len(j.ext) else "")) \
                or s.lang or ""
            if lang and lang != "und":
                bits.append(lang)
            L.append(" ".join(bits))
        af = _arg_after(cmd, "-af")
        if af:
            L.append(f" Filters:     {af}")
        L.append(LINE)
    if pl["subs"]:
        L.append("Subtitles:")
        for s, how in pl["subs"]:
            no = J.ext_no(s)
            src = f"#ext{no + 1}" if no is not None else f"#{s.index}"
            L.append(f" Stream {src}: {P.codec_name(s).upper()} > {'COPY' if how == 'copy' else how.upper()}"
                     + (f" {s.lang}" if s.lang and s.lang != 'und' else ""))
        L.append(LINE)
    for w in dict.fromkeys(pl.get("warnings", [])):
        L.append(f"Warning: {tr(w) if tr else w}")
    if pl.get("warnings"):
        L.append(LINE)
    # ── each ffmpeg run
    runs = phases(lines, kinds)
    n_pass1 = sum(1 for p in runs if p["kind"] == "pass1")
    k1 = 0
    for p in runs:
        title = PHASE_TITLE.get(p["kind"], p["kind"].upper())
        if p["kind"] == "pass1":
            k1 += 1
            title = f"PASS {k1} OF {n_pass1 + 1} (ANALYSIS)"
        elif p["kind"] == "main" and n_pass1:
            title = f"PASS {n_pass1 + 1} OF {n_pass1 + 1} (ENCODING)"
        L.append(title + (f" STARTED {stamp(p['started'])}" if p["started"] else ""))
        if p["cmd"]:
            L.append("Command: " + p["cmd"])
        L.append("Encoding in progress...")
        L += [ln for ln in p["lines"] if ln.strip() and not NOISE.search(ln)]
        L.append(LINE)
    # ── the statistics of the main run
    if main is not None:
        sx = stats(main["lines"])
        if sx:
            L.append("Statistics:")
            L += [" " + x for x in sx]
            L.append(LINE)
    if j.status == "done":
        opts = encoder_options(j.out) if j.out and os.path.isfile(j.out) else []
        if opts:
            L.append("Encoder settings (written into the stream):")
            L += [" " + o for o in opts]
            L.append(LINE)
    if j.metrics:
        L.append("Quality metrics: " + ", ".join(f"{k.upper()} {v:.4g}" for k, v in j.metrics.items()))
        L.append(LINE)
    # ── the end
    word = {"done": "COMPLETED!", "stop": "STOPPED!", "error": "FAILED!"}.get(j.status, j.status.upper() + "!")
    L.append(f"ENCODING {word} {stamp(ended)}")
    if j.status == "error" and j.error:
        L.append(f"Error: {j.error}")
    took = max(0.0, ended - started)
    speed = ""
    od = J.out_duration(j) if j.info is not None and not j.info.error else 0
    if took > 0 and od and j.status == "done":
        speed = f" (speed {od / took:.2f}x)"
    L.append(f"Encoding time: {dur(took)}{speed}")
    if peak_mem:
        L.append(f"Maximum memory usage: {peak_mem / (1 << 20):.1f} MiB")
    files = (j.parts or [j.out]) if j.status == "done" else []
    total = sum(os.path.getsize(f) for f in files if f and os.path.isfile(f))
    if main is not None:
        sz = sizes(main["lines"])
        if sz:
            enc = int((sz["video"] + sz["audio"] + sz["subtitle"] + sz["other"]) * 1024)
            L.append(f"Encoded data size: {size_text(enc)} — video {sz['video']:.0f} KiB, "
                     f"audio {sz['audio']:.0f} KiB, subtitles {sz['subtitle']:.0f} KiB")
            over = f"muxing overhead {sz['overhead']}%" if sz["overhead"] != "unknown" else ""
            if total and len(files) == 1 and total >= enc:
                L.append(f"Container data size: {size_text(total - enc)}" + (f" ({over})" if over else ""))
            elif over:
                L.append(f"Container data size: {over}")
    if j.status == "done":
        L.append(f"Total file size: {size_text(total)}")
        if od:
            L.append(f"Average bitrate: {total * 8 / od / 1000:.0f} kbps")
        if len(files) > 1:
            L.append(f"Encoded files ({len(files)}):")
            L += [" " + f for f in files]
        else:
            L.append(f"Encoded file path: {files[0]}")
    L.append(LINE)
    return "\n".join(L) + "\n"
