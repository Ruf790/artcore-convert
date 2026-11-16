"""Test helper (1.8): a tiny Blu-ray folder — two m2ts clips, two playlists (MPLS) with languages and marks."""
import json
import os
import struct
import subprocess
import sys


def mpls(clips, streams, marks):
    """clips: [(name, in, out)], streams: [(kind, pid, coding, lang)], marks: [(item, t)]"""
    def stn():
        body = b""
        counts = {"video":0,"audio":0,"subtitle":0}
        for kind in ("video","audio","subtitle"):
            for k, pid, coding, lang in streams:
                if k != kind: continue
                counts[kind]+=1
                entry = bytes([1]) + struct.pack(">H", pid) + b"\0"*6
                if kind=="video": attr = bytes([coding, 0x61])
                elif kind=="audio": attr = bytes([coding, 0x31]) + lang.encode()
                else: attr = bytes([coding]) + lang.encode()
                body += bytes([len(entry)]) + entry + bytes([len(attr)]) + attr
        head = struct.pack(">HH", 0, 0) + bytes([counts["video"],counts["audio"],counts["subtitle"],0,0,0,0]) + b"\0"*5
        data = head + body
        return struct.pack(">H", len(data)-2) + data[2:]
    items = b""
    for name, a, b in clips:
        it = name.encode() + b"M2TS" + struct.pack(">H", 1) + b"\0" + struct.pack(">II", int(a*45000), int(b*45000)) + b"\0"*8 + b"\0\0\0\0" + stn()
        items += struct.pack(">H", len(it)) + it
    pl = struct.pack(">IHHH", 0, 0, len(clips), 0) + items
    mk = struct.pack(">IH", 0, len(marks)) + b"".join(bytes([0,1]) + struct.pack(">HIHI", i, int(t*45000), 0xFFFF, 0) for i, t in marks)
    head_len = 40
    pl_start = head_len; mk_start = pl_start + len(pl)
    head = b"MPLS0200" + struct.pack(">III", pl_start, mk_start, 0) + b"\0"*(head_len-20)
    return head + pl + mk

def make(d):
    os.makedirs(f"{d}/BDMV/PLAYLIST", exist_ok=True); os.makedirs(f"{d}/BDMV/STREAM", exist_ok=True)
    starts = []
    for i, (f1, dur) in enumerate(((440, 6), (660, 5))):
        out = f"{d}/BDMV/STREAM/0000{i+1}.m2ts"
        subprocess.run(["ffmpeg","-v","error","-y","-f","lavfi","-i",f"testsrc2=s=640x360:r=24:d={dur}","-f","lavfi","-i",f"sine=f={f1}:d={dur}","-f","lavfi","-i",f"sine=f={f1+200}:d={dur}",
            "-map","0","-map","1","-map","2","-c:v","libx264","-preset","ultrafast","-g","24","-c:a","ac3","-mpegts_m2ts_mode","1","-streamid","0:0x1011","-streamid","1:0x1100","-streamid","2:0x1101","-f","mpegts",out],check=True)
        j = json.loads(subprocess.run(["ffprobe","-v","error","-of","json","-show_format",out],capture_output=True,text=True).stdout)
        s = float(j["format"]["start_time"]); starts.append((s, float(j["format"]["duration"])))
    clips = [("00001", starts[0][0], starts[0][0]+6.0), ("00002", starts[1][0], starts[1][0]+5.0)]
    st = [("video",0x1011,0x1B,""),("audio",0x1100,0x81,"rus"),("audio",0x1101,0x81,"eng")]
    open(f"{d}/BDMV/PLAYLIST/00800.mpls","wb").write(mpls(clips, st, [(0, starts[0][0]), (0, starts[0][0]+3), (1, starts[1][0]+1)]))
    open(f"{d}/BDMV/PLAYLIST/00001.mpls","wb").write(mpls([("00002", starts[1][0], starts[1][0]+5.0)], st, []))
    return starts


if __name__ == "__main__":
    make(sys.argv[1])
