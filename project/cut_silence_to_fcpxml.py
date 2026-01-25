import argparse, os, re, subprocess, sys, math
from dataclasses import dataclass
from typing import List, Tuple, Optional


@dataclass
class SilenceInterval:
    start: float
    end: float


def run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def require_tool(name: str) -> None:
    p = run([name, "-version"])
    if p.returncode != 0:
        raise FileNotFoundError(f'Could not run "{name}". Make sure it is installed and on PATH.')


def get_duration_seconds(path: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
    p = run(cmd)
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError(f"ffprobe failed to read duration for: {path}\n{p.stderr}")
    return float(p.stdout.strip())


def ffprobe_fields(path: str, select: str, entries: str) -> dict:
    cmd = ["ffprobe", "-v", "error", "-select_streams", select, "-show_entries", entries, "-of", "default=noprint_wrappers=1", path]
    p = run(cmd)
    if p.returncode != 0:
        return {}
    out = {}
    for line in p.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def get_media_info(path: str) -> dict:
    # Video stream
    v = ffprobe_fields(path, "v:0", "stream=width,height,avg_frame_rate,r_frame_rate,sample_aspect_ratio,field_order")
    # Audio stream
    a = ffprobe_fields(path, "a:0", "stream=sample_rate,channels,channel_layout")
    info = {}
    info.update(v)
    info.update(a)
    return info


def parse_rate(rate_str: str) -> float:
    if not rate_str or "/" not in rate_str:
        return 30.0
    n, d = rate_str.split("/", 1)
    n = float(n)
    d = float(d) if float(d) != 0 else 1.0
    return n / d


def fps_to_timebase_ntsc_and_real_fps(fps: float) -> Tuple[int, bool, float]:
    # FCP7 uses integer timebase plus ntsc flag for 29.97/59.94.
    if abs(fps - (30000 / 1001)) < 0.05 or abs(fps - 29.97) < 0.05:
        return 30, True, (30 / 1.001)
    if abs(fps - (60000 / 1001)) < 0.05 or abs(fps - 59.94) < 0.05:
        return 60, True, (60 / 1.001)
    tb = max(1, int(round(fps)))
    return tb, False, float(tb)


def sec_to_frames(sec: float, fps_real: float) -> int:
    return int(round(sec * fps_real))


def create_mono_proxy(input_path: str, mono_path: str, sample_rate: int = 48000) -> None:
    # Video: copy. Audio: unambiguous mono PCM in MOV (Premiere-friendly).
    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", input_path, "-c:v", "copy", "-c:a", "pcm_s16le", "-ac", "1", "-ar", str(sample_rate), mono_path]
    p = run(cmd)
    if p.returncode != 0 or not os.path.isfile(mono_path) or os.path.getsize(mono_path) == 0:
        raise RuntimeError(f"Failed to create mono proxy.\nCommand: {' '.join(cmd)}\n\n{p.stderr}")


def run_ffmpeg_silencedetect(path: str, threshold_db: float, min_silence: float, audio_stream: Optional[str]) -> str:
    # Detect on ORIGINAL, but force mono during analysis for stability.
    cmd = ["ffmpeg", "-hide_banner", "-i", path]
    if audio_stream:
        cmd += ["-map", audio_stream]
    cmd += ["-vn", "-ac", "1", "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence}", "-f", "null", "-"]
    p = run(cmd)
    if not p.stderr.strip():
        raise RuntimeError("ffmpeg produced no silencedetect output; check ffmpeg install and input file.")
    return p.stderr


def parse_silences(ffmpeg_stderr: str) -> List[SilenceInterval]:
    starts = []
    intervals: List[SilenceInterval] = []
    start_re = re.compile(r"silence_start:\s*([0-9]*\.?[0-9]+)")
    end_re = re.compile(r"silence_end:\s*([0-9]*\.?[0-9]+)")

    for line in ffmpeg_stderr.splitlines():
        m1 = start_re.search(line)
        if m1:
            starts.append(float(m1.group(1)))
            continue
        m2 = end_re.search(line)
        if m2 and starts:
            s = starts.pop(0)
            e = float(m2.group(1))
            if e > s:
                intervals.append(SilenceInterval(s, e))

    for s in starts:
        intervals.append(SilenceInterval(s, math.inf))

    intervals.sort(key=lambda x: x.start)
    return intervals


def merge_overlaps(intervals: List[SilenceInterval], duration: float) -> List[SilenceInterval]:
    fixed = []
    for iv in intervals:
        s = max(0.0, iv.start)
        e = duration if math.isinf(iv.end) else min(duration, iv.end)
        if e > s:
            fixed.append(SilenceInterval(s, e))
    fixed.sort(key=lambda x: x.start)

    merged: List[SilenceInterval] = []
    for iv in fixed:
        if not merged:
            merged.append(iv)
            continue
        last = merged[-1]
        if iv.start <= last.end:
            last.end = max(last.end, iv.end)
        else:
            merged.append(iv)
    return merged


def invert_to_keeps(silences: List[SilenceInterval], duration: float, pad: float, min_keep: float) -> List[Tuple[float, float]]:
    removes: List[SilenceInterval] = []
    for iv in silences:
        rs = max(0.0, iv.start - pad)
        re_ = duration if math.isinf(iv.end) else min(duration, iv.end + pad)
        if re_ > rs:
            removes.append(SilenceInterval(rs, re_))
    removes = merge_overlaps(removes, duration)

    keeps: List[Tuple[float, float]] = []
    cursor = 0.0
    for iv in removes:
        if iv.start > cursor:
            ks, ke = cursor, iv.start
            if (ke - ks) >= min_keep:
                keeps.append((ks, ke))
        cursor = max(cursor, iv.end)

    if duration > cursor and (duration - cursor) >= min_keep:
        keeps.append((cursor, duration))

    return keeps


def sar_to_par(sar: str) -> Tuple[int, int]:
    # sample_aspect_ratio like "1:1" or "4:3" or "0:1"
    if not sar or ":" not in sar:
        return (1, 1)
    a, b = sar.split(":", 1)
    try:
        a_i = int(a)
        b_i = int(b)
        if a_i <= 0 or b_i <= 0:
            return (1, 1)
        return (a_i, b_i)
    except:
        return (1, 1)


def field_order_to_fcp(field: str) -> str:
    # ffprobe field_order can be: progressive, tt, bb, tb, bt, unknown
    # FCP7 wants: none / upper / lower (common). We'll map best-effort.
    f = (field or "").lower()
    if f == "progressive" or f == "unknown" or f == "":
        return "none"
    # Many sources use top-first as "tt" or "tb"
    if f in ("tt", "tb"):
        return "upper"
    if f in ("bb", "bt"):
        return "lower"
    return "none"


def make_fcp7_xml(link_media_path: str, keeps: List[Tuple[float, float]], seq_name: str) -> str:
    # IMPORTANT: link_media_path should be the mono proxy so Premiere can relink.
    dur = get_duration_seconds(link_media_path)
    info = get_media_info(link_media_path)

    width = int(info.get("width", "1920"))
    height = int(info.get("height", "1080"))
    fps = parse_rate(info.get("avg_frame_rate") or info.get("r_frame_rate") or "30/1")
    timebase, ntsc, fps_real = fps_to_timebase_ntsc_and_real_fps(fps)

    sample_rate = int(info.get("sample_rate", "48000"))
    channels = 1  # forced mono proxy
    sar = info.get("sample_aspect_ratio", "1:1")
    par_n, par_d = sar_to_par(sar)
    field = field_order_to_fcp(info.get("field_order", "progressive"))

    abs_path = os.path.abspath(link_media_path).replace("\\", "/")
    pathurl = "file:///" + abs_path

    v_track_items = []
    a_track_items = []

    timeline_cursor = 0.0
    for i, (ks, ke) in enumerate(keeps, start=1):
        seg_dur = max(0.0, ke - ks)
        if seg_dur <= 0:
            continue

        v_id = f"clipitem-v{i}"
        a_id = f"clipitem-a{i}"

        start_f = sec_to_frames(timeline_cursor, fps_real)
        end_f = sec_to_frames(timeline_cursor + seg_dur, fps_real)
        in_f = sec_to_frames(ks, fps_real)
        out_f = sec_to_frames(ke, fps_real)

        file_block = f"""
            <file id="file-1">
              <name>{os.path.basename(link_media_path)}</name>
              <pathurl>{pathurl}</pathurl>
              <rate>
                <timebase>{timebase}</timebase>
                <ntsc>{"TRUE" if ntsc else "FALSE"}</ntsc>
              </rate>
              <duration>{sec_to_frames(dur, fps_real)}</duration>
              <media>
                <video>
                  <samplecharacteristics>
                    <width>{width}</width>
                    <height>{height}</height>
                    <anamorphic>FALSE</anamorphic>
                    <pixelaspectratio>{par_n}/{par_d}</pixelaspectratio>
                    <fielddominance>{field}</fielddominance>
                  </samplecharacteristics>
                </video>
                <audio>
                  <samplecharacteristics>
                    <samplerate>{sample_rate}</samplerate>
                    <channels>{channels}</channels>
                  </samplecharacteristics>
                </audio>
              </media>
            </file>
        """.strip()

        v_item = f"""
          <clipitem id="{v_id}">
            <name>{seq_name}_{i:03d}</name>
            <enabled>TRUE</enabled>
            <start>{start_f}</start>
            <end>{end_f}</end>
            <in>{in_f}</in>
            <out>{out_f}</out>
            {file_block}
            <link>
              <linkclipref>{v_id}</linkclipref>
              <mediatype>video</mediatype>
              <trackindex>1</trackindex>
              <clipindex>{i}</clipindex>
            </link>
            <link>
              <linkclipref>{a_id}</linkclipref>
              <mediatype>audio</mediatype>
              <trackindex>1</trackindex>
              <clipindex>{i}</clipindex>
            </link>
          </clipitem>
        """

        a_item = f"""
          <clipitem id="{a_id}">
            <name>{seq_name}_{i:03d}</name>
            <enabled>TRUE</enabled>
            <start>{start_f}</start>
            <end>{end_f}</end>
            <in>{in_f}</in>
            <out>{out_f}</out>
            <file id="file-1"/>
            <sourcetrack>
              <mediatype>audio</mediatype>
              <trackindex>1</trackindex>
            </sourcetrack>
            <link>
              <linkclipref>{v_id}</linkclipref>
              <mediatype>video</mediatype>
              <trackindex>1</trackindex>
              <clipindex>{i}</clipindex>
            </link>
            <link>
              <linkclipref>{a_id}</linkclipref>
              <mediatype>audio</mediatype>
              <trackindex>1</trackindex>
              <clipindex>{i}</clipindex>
            </link>
          </clipitem>
        """

        v_track_items.append(v_item)
        a_track_items.append(a_item)
        timeline_cursor += seg_dur

    if not v_track_items:
        v_track_items.append("""
          <gap>
            <name>Empty</name>
            <duration>1</duration>
          </gap>
        """)

    # Sequence-level format settings (this is what makes Premiere create a matching sequence)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
  <sequence>
    <name>{seq_name}</name>
    <rate>
      <timebase>{timebase}</timebase>
      <ntsc>{"TRUE" if ntsc else "FALSE"}</ntsc>
    </rate>
    <media>
      <video>
        <format>
          <samplecharacteristics>
            <rate>
              <timebase>{timebase}</timebase>
              <ntsc>{"TRUE" if ntsc else "FALSE"}</ntsc>
            </rate>
            <width>{width}</width>
            <height>{height}</height>
            <anamorphic>FALSE</anamorphic>
            <pixelaspectratio>{par_n}/{par_d}</pixelaspectratio>
            <fielddominance>{field}</fielddominance>
          </samplecharacteristics>
        </format>
        <track>
          {''.join(v_track_items)}
        </track>
      </video>
      <audio>
        <format>
          <samplecharacteristics>
            <samplerate>{sample_rate}</samplerate>
            <channels>{channels}</channels>
          </samplecharacteristics>
        </format>
        <track>
          {''.join(a_track_items)}
        </track>
      </audio>
    </media>
  </sequence>
</xmeml>
"""
    return xml


def main(argv=None):
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="Input video/audio file")
    parser.add_argument("--threshold", type=float, default=-35)
    parser.add_argument("--min_silence", type=float, default=0.25)
    parser.add_argument("--pad", type=float, default=0.08)
    parser.add_argument("--min_keep", type=float, default=0.10)
    parser.add_argument("--audio_stream", default=None)
    parser.add_argument("--regen_mono", action="store_true")
    args = parser.parse_args(argv)

    require_tool("ffmpeg")
    require_tool("ffprobe")

    if not os.path.isfile(args.input):
        print("Input file not found:", args.input, file=sys.stderr)
        sys.exit(1)

    in_dir = os.path.dirname(os.path.abspath(args.input))
    base = os.path.splitext(os.path.basename(args.input))[0]

    # Make a uniquely named proxy so Premiere doesn't “helpfully” match the stereo original by name.
    mono_path = os.path.join(in_dir, f"__SILENCECUT_MONO_PROXY__{base}.mov")

    if args.regen_mono or not os.path.isfile(mono_path) or os.path.getsize(mono_path) == 0:
        print("Creating mono proxy:", mono_path)
        create_mono_proxy(args.input, mono_path, sample_rate=48000)
    else:
        print("Using existing mono proxy:", mono_path)

    # Detect on original (cuts behave like before), but -ac 1 in the detection step for stability
    orig_duration = get_duration_seconds(args.input)
    stderr = run_ffmpeg_silencedetect(args.input, args.threshold, args.min_silence, args.audio_stream)
    silences_raw = parse_silences(stderr)
    silences = merge_overlaps(silences_raw, orig_duration)
    keeps = invert_to_keeps(silences, orig_duration, args.pad, args.min_keep)

    print(f"Detected silences: {len(silences)} | Kept segments: {len(keeps)}")

    seq_name = f"{base}_NoSilence"
    out_xml = os.path.join(in_dir, f"{base}__nosilence.XML")

    # Link XML to the mono proxy so Premiere can relink without channel-type errors
    xml = make_fcp7_xml(mono_path, keeps, seq_name)
    with open(out_xml, "w", encoding="utf-8", newline="\n") as f:
        f.write(xml)

    kept_total = sum(max(0.0, b - a) for a, b in keeps)
    print("Wrote:", out_xml)
    print("Linked media (mono proxy):", mono_path)
    print(f"Original duration: {orig_duration/60:.3f}m | Kept: {kept_total/60:.3f}m | Segments: {len(keeps)}")
    print("Import the .XML into Premiere. It should create a sequence matching the proxy (resolution/fps/etc).")

if __name__ == "__main__":
    raise SystemExit(main())