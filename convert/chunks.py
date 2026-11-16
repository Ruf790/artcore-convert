"""1.8: encoding in chunks («clusters») — the film's picture cut into pieces of a few minutes, each piece read,
decoded, filtered and encoded by its own ffmpeg, several at a time, so the CPU is used in full whatever the
encoder; then the pieces are joined without encoding again, the sound and subtitles are made by one more ffmpeg
and both are put into the result. Every finished piece stays on disk (data\\chunks\\<key>) until the result is
done: a stopped, failed or crashed task goes on from the first piece not yet made, and the same file with the same
settings reuses the pieces it already has.

Not in chunks (they need the whole film in one run): two passes, the two-pass stabilisation, joined files, copied
or no video, GIF."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import time

from PySide6.QtCore import QObject, Signal

from . import config
from . import encoders as E
from . import job as J
from .runner import Runner, _error_line, _q

KEEP_DAYS = 14


def root() -> str:
    d = os.path.join(config.data_dir(), "chunks")
    os.makedirs(d, exist_ok=True)
    return d


def purge(days: int = KEEP_DAYS) -> None:
    """Chunks of tasks left unfinished for <days> go."""
    try:
        now = time.time()
        for name in os.listdir(root()):
            p = os.path.join(root(), name)
            if os.path.isdir(p) and now - os.path.getmtime(p) > days * 86400:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def why_not(job: J.Job, av: E.Available | None) -> str:
    """"" when the task can be encoded in chunks, else the i18n key of the reason."""
    if job.info is None or job.info.error or job.info.video is None:
        return "chk_no_video"
    v = job.settings.get("video", {})
    c = E.container(job.settings.get("container", "mkv"))
    if v.get("mode") in ("copy", "none") or c.audio_only:
        return "chk_no_video"
    enc = av.resolve(v.get("encoder", "auto_hevc")) if av is not None else E.BY_ID.get(v.get("encoder", ""))
    if enc is None or enc.id == "gif":
        return "chk_no_video"
    if job.joins:
        return "chk_joins"
    if av is not None and J.two_pass(job, enc, av):
        return "chk_two_pass"
    if J.needs_vidstab(job):
        return "chk_vidstab"
    if enc is not None and J.dyn_hdr(job, enc, J.out_bits(job, enc))["hdr10p"]:
        return "chk_hdr10p"
    return ""


def workers_auto(enc: E.VEnc | None) -> int:
    n = os.cpu_count() or 2
    if enc is not None and enc.hw:
        return 2                                          # a graphics card has one or two encoders
    return max(2, min(8, n // 4))


def plan(job: J.Job, minutes: float) -> list[dict]:
    """The chunks: [{n, pieces: [[a, b], …] in the source's time, dur}] — the result's timeline cut every
    <minutes>, each cut on the start of a frame of the source (ffmpeg then puts that frame in the later chunk
    only: no frame lost or doubled between two chunks)."""
    segs = J.segments(job) or [(0.0, J.src_duration(job))]
    total = sum(b - a for a, b in segs)
    step = max(10.0, float(minutes) * 60.0)
    fps = J.out_fps(job) or (job.info.video.fps if job.info and job.info.video else 0) or 25.0
    if total <= step * 1.5:
        cuts = [0.0, total]
    else:
        n = max(2, round(total / step))
        cuts = [0.0] + [total * i / n for i in range(1, n)] + [total]
    out = []
    for i in range(len(cuts) - 1):
        lo, hi = cuts[i], cuts[i + 1]
        pcs, pos = [], 0.0
        for a, b in segs:
            s0, s1 = pos, pos + (b - a)                   # this source piece on the result's timeline
            x, y = max(lo, s0), min(hi, s1)
            if y > x + 1e-6:
                xa, ya = a + (x - s0), a + (y - s0)
                if x > s0 + 1e-6:                         # a cut inside the piece: on a frame of the source
                    xa = round(xa * fps) / fps
                if y < s1 - 1e-6:
                    ya = round(ya * fps) / fps
                pcs.append([round(xa, 6), round(ya, 6)])
            pos = s1
        out.append({"n": i + 1, "pieces": pcs, "dur": round(sum(b - a for a, b in pcs), 6)})
    return out


def key(job: J.Job, minutes: float, enc_id: str) -> str:
    """The same file, settings, cut, crop and chunk length → the same chunks (reused)."""
    try:
        stt = os.stat(job.src)
        fid = [os.path.abspath(job.src), stt.st_size, int(stt.st_mtime)]
    except OSError:
        fid = [os.path.abspath(job.src)]
    from . import hardsub as HS
    b = HS.active(job)                                    # 2.0: burnt-in subtitles are in the picture
    burn = [b.key(), b.mode, job.ext[b.ext].get("path") if b.ext is not None else "",
            job.ext[b.ext].get("delay") if b.ext is not None else 0, job.settings.get("hardsub")] if b else []
    data = json.dumps([fid, job.settings.get("video"), job.settings.get("filters"), job.settings.get("venc"),
                       job.settings.get("fmt"), job.settings.get("container"), job.trim, job.trim_cut,
                       job.crop_found, minutes, enc_id, burn], sort_keys=True, default=str)
    return hashlib.sha1(data.encode("utf-8")).hexdigest()[:16]


def chunk_job(job: J.Job, pieces: list) -> J.Job:
    """The task as one chunk: only these pieces of the picture — no sound, subtitles, chapters or tags."""
    k = copy.copy(job)
    k.settings = copy.deepcopy(job.settings)
    k.settings["meta"] = False
    k.settings["split"] = {"mode": "off"}
    k.trim, k.trim_cut, k.joins, k.chapters = [list(p) for p in pieces], False, [], []
    k.ext = [dict(e) for e in job.ext if e.get("kind") == "subtitle" and e.get("burn")]    # 2.0: burnt in
    k.meta = {}
    info = job.info
    k.tracks = {str(s.index): {"keep": False} | ({"burn": job.tracks[str(s.index)]["burn"]}
                                                  if "burn" in job.tracks.get(str(s.index), {}) else {})
                for s in (info.of("audio") + info.of("subtitle") if info else [])}
    from . import hardsub as HS
    HS.pin(job, k)
    return k


def rest_job(job: J.Job) -> J.Job | None:
    """The task without its picture: the sound, subtitles, chapters and tags, cut the same way (and encoded
    where the whole task would encode them). None: nothing but the picture."""
    k = copy.copy(job)
    k.settings = copy.deepcopy(job.settings)
    k.settings.setdefault("video", {})["mode"] = "none"
    k.settings["split"] = {"mode": "off"}
    k.force_recut = bool(J.segments(job))
    p = J.plan(k)
    return k if (p["audio"] or p["subs"]) else None


def concat_list(path: str, files: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for x in files:
            f.write("file '" + x.replace("\\", "/").replace("'", "'\\''") + "'\n")


class ChunkRunner(QObject):
    """Runs one task in chunks; the same signals and controls as Runner, so a window slot can use either."""
    progress = Signal(dict)
    log = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.kids: dict[int, Runner] = {}                # chunk number → its ffmpeg
        self.tail = Runner(self)                          # the join, the sound, the result, the metrics
        self.tail.progress.connect(self._tail_progress)
        self.tail.log.connect(self._tail_log)
        self.tail.finished.connect(self._tail_done)
        self.cancelled = False
        self.paused = False
        self.post_failed = False
        self.out = ""
        self.stage = ""
        self._kind = "chunk"
        self.priority = "below"
        self.cores: list[int] | None = None
        self.started = 0.0
        self.paused_for = 0.0
        self._paused_at = 0.0
        self.dir = ""
        self.reused = 0

    # ── what the window asks ───────────────────────────────────
    @property
    def running(self) -> bool:
        return bool(self.kids) or self.tail.running

    @property
    def kind(self) -> str:
        return self.tail.kind if self.stage == "tail" else self._kind

    @property
    def proc(self):
        for r in self.kids.values():
            if r.proc is not None:
                return r.proc
        return self.tail.proc

    def pids(self) -> list[int]:
        out = []
        for r in list(self.kids.values()) + [self.tail]:
            if r.proc is not None and r.proc.processId():
                out.append(int(r.proc.processId()))
        return out

    def elapsed(self) -> float:
        if not self.started:
            return 0.0
        now = self._paused_at if self.paused else time.monotonic()
        return max(0.0, now - self.started - self.paused_for)

    def pause(self, on: bool) -> bool:
        if self.paused == on or not self.running:
            return False
        for r in list(self.kids.values()) + [self.tail]:
            if r.running:
                r.pause(on)
        self.paused = on
        if on:
            self._paused_at = time.monotonic()
        else:
            self.paused_for += time.monotonic() - self._paused_at
        return True

    def cancel(self):
        if not self.running:
            return
        self.cancelled = True
        self.queue = []
        for r in list(self.kids.values()):
            r.cancel()
        if self.tail.running:
            self.tail.cancel()

    def set_cores(self, cores):
        self.cores = list(cores) if cores else None
        for r in list(self.kids.values()) + [self.tail]:
            r.set_cores(self.cores)

    def set_priority(self, level: str):
        self.priority = level
        for r in list(self.kids.values()) + [self.tail]:
            r.set_priority(level)

    # ── the run ────────────────────────────────────────────────
    def start_job(self, job: J.Job, ffmpeg: str, out: str, av: E.Available, hw: bool, *, workers: int = 0,
                  minutes: float = 3.0, priority: str = "below", post=None, cores=None, threads: int = 0,
                  post_kinds=None):
        self.job, self.ffmpeg, self.av, self.hw = job, ffmpeg, av, hw
        self.out, self.priority, self.cores = out, priority, (list(cores) if cores else None)
        self.post = [list(a) for a in (post or [])]
        self.post_kinds = list(post_kinds or ["metrics"] * len(self.post))
        self.cancelled = self.paused = self.post_failed = False
        self.started, self.paused_for = time.monotonic(), 0.0
        self.stage = "chunks"
        enc = av.resolve(job.settings.get("video", {}).get("encoder", "auto_hevc"))
        self.workers = max(1, int(workers or workers_auto(enc)))
        total_threads = threads or (os.cpu_count() or 1)
        self.threads = max(1, total_threads // self.workers)
        self.chunks = plan(job, minutes)
        self.total = sum(c["dur"] for c in self.chunks) or 1.0
        self.dir = os.path.join(root(), key(job, minutes, enc.id if enc else ""))
        os.makedirs(self.dir, exist_ok=True)
        self.ext = "." + J.out_ext(job.settings)
        with open(os.path.join(self.dir, "plan.json"), "w", encoding="utf-8") as f:
            json.dump({"src": job.src, "chunks": self.chunks, "made": time.time()}, f)
        self.done = {c["n"] for c in self.chunks if os.path.isfile(self._file(c["n"]))}
        self.reused = len(self.done)
        self.queue = [c for c in self.chunks if c["n"] not in self.done]
        self.live: dict[int, dict] = {}
        self.buf: dict[int, list[str]] = {}
        self._kind = "resume"                             # what the log says first: how many, how many reused
        self.log.emit(f"$ resume {self.reused} {len(self.chunks)} {self.workers}")
        self._fill()

    def _file(self, n: int) -> str:
        return os.path.join(self.dir, f"chunk_{n:04d}{self.ext}")

    def _fill(self):
        if self.cancelled:
            return
        while self.queue and len(self.kids) < self.workers:
            c = self.queue.pop(0)
            k = chunk_job(self.job, c["pieces"])
            out = self._file(c["n"])
            try:
                args = J.build_args(k, self.ffmpeg, out, self.av, self.hw, threads=self.threads)
            except (RuntimeError, OSError) as e:
                self._fail(f"chunk {c['n']}: {e}")
                return
            r = Runner(self)
            self.kids[c["n"]] = r
            self.buf[c["n"]] = []
            self.live[c["n"]] = {}
            r.progress.connect(lambda d, n=c["n"]: self._kid_progress(n, d))
            r.log.connect(lambda line, n=c["n"]: self.buf[n].append(line))
            r.finished.connect(lambda ok, err, n=c["n"]: self._kid_done(n, ok, err))
            r.start(args, out, c["dur"], self.priority, cores=self.cores)
        if not self.kids and not self.queue and self.stage == "chunks":
            self._join()

    def _kid_progress(self, n: int, d: dict):
        self.live[n] = d
        self._emit()

    def _emit(self, tail_frac: float = 0.0):
        made = sum(c["dur"] for c in self.chunks if c["n"] in self.done)
        now = sum(min(float(d.get("out_time", 0) or 0), next((c["dur"] for c in self.chunks if c["n"] == n), 0))
                  for n, d in self.live.items() if n in self.kids)
        part = min(1.0, (made + now) / self.total)
        frac = min(0.999, 0.94 * part + 0.06 * tail_frac)
        speed = sum(float(d.get("speed", 0) or 0) for n, d in self.live.items() if n in self.kids)
        fps = sum(float(d.get("fps", 0) or 0) for n, d in self.live.items() if n in self.kids)
        size = sum(os.path.getsize(self._file(n)) for n in self.done if os.path.isfile(self._file(n)))
        size += sum(int(d.get("size", 0) or 0) for n, d in self.live.items() if n in self.kids)
        left = self.total - made - now
        self.progress.emit({"frac": frac, "out_time": made + now, "fps": fps, "speed": speed,
                            "bitrate": "", "size": size, "eta": left / speed if speed > 0 and left > 0 else 0.0,
                            "elapsed": self.elapsed(), "frame": 0,
                            "chunks": f"{len(self.done)}/{len(self.chunks)}", "workers": len(self.kids)})

    def _kid_done(self, n: int, ok: bool, err: str):
        r = self.kids.pop(n, None)
        lines = self.buf.pop(n, [])
        self.live.pop(n, None)
        if lines:
            self._kind = "chunk"
            self.log.emit(lines[0] if lines[0].startswith("$ ") else "$ chunk")
            for ln in (lines[1:] if lines[0].startswith("$ ") else lines):
                self.log.emit(ln)
        if r is not None:
            r.deleteLater()
        if self.stage != "chunks":                        # failed already: the others only stop
            return
        if self.cancelled:
            if not self.kids:
                self.stage = ""
                self.finished.emit(False, "")
            return
        if not ok:
            self._fail(f"chunk {n}: " + (err or "ffmpeg"))
            return
        self.done.add(n)
        self._emit()
        self._fill()

    def _fail(self, err: str):
        self.stage = ""
        self.queue = []
        for r in list(self.kids.values()):
            r.cancel()
        self.finished.emit(False, err)

    def _join(self):
        """All chunks made: join them, make the sound and subtitles, put both into the result (then the
        metrics) — one after another, by the ordinary runner."""
        self.stage = "tail"
        files = [self._file(c["n"]) for c in self.chunks]
        lst = os.path.join(self.dir, "chunks.txt")
        concat_list(lst, files)
        video = os.path.join(self.dir, "video.mkv")
        head = [self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "verbose", "-nostats", "-progress", "pipe:1"]
        pre = [head + ["-f", "concat", "-safe", "0", "-i", lst, "-map", "0:v", "-c", "copy", "-f", "matroska", video]]
        kinds = ["concat"]
        rest = rest_job(self.job)
        rest_file = ""
        if rest is not None:
            rest_file = os.path.join(self.dir, "rest" + self.ext)
            try:
                pre.append(J.build_args(rest, self.ffmpeg, rest_file, self.av, self.hw, threads=self.threads * self.workers))
                kinds.append("rest")
            except (RuntimeError, OSError):
                rest_file = ""
        c = E.container(self.job.settings.get("container", "mkv"))
        main = head + ["-i", video] + (["-i", rest_file] if rest_file else []) + ["-map", "0:v:0"]
        if rest_file:
            main += ["-map", "1", "-map_metadata", "1", "-map_chapters", "1"]
        main += ["-c", "copy"]
        if c.id in ("mp4", "mov", "m4v"):
            main += ["-movflags", "+faststart"]
        main += (["-f", c.fmt] if c.fmt else []) + [self.out]
        kinds += ["main"] + self.post_kinds
        self.tail.start(main, self.out, J.out_duration(self.job), self.priority, pre=pre, post=self.post,
                        cores=self.cores, kinds=kinds)

    def _tail_progress(self, d: dict):
        self._emit(float(d.get("frac", 0) or 0))

    def _tail_log(self, line: str):
        self.log.emit(line)

    def _tail_done(self, ok: bool, err: str):
        self.post_failed = self.tail.post_failed
        self.stage = ""
        if ok and not self.cancelled:
            shutil.rmtree(self.dir, ignore_errors=True)  # done: its chunks are no longer needed
        self.finished.emit(ok and not self.cancelled, "" if self.cancelled else err)

    def describe(self) -> str:
        return f"{len(self.chunks)} × {self.workers}"


__all__ = ["ChunkRunner", "why_not", "plan", "key", "purge", "workers_auto", "_error_line", "_q"]
