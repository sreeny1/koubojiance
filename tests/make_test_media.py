# -*- coding: utf-8 -*-
"""生成端到端测试视频：SAPI 中文 TTS 合成口播 → ffmpeg 合成 mp4。

视频1：广告法极限词 + 平台导流词
视频2：金融风险词 + 干净句子
生成位置：tests/media/test_video_1.mp4 / test_video_2.mp4
"""
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
MEDIA = BASE / "media"
MEDIA.mkdir(parents=True, exist_ok=True)

SCRIPTS = {
    "test_video_1": [
        "大家好，今天给大家介绍一款效果最好的面霜。",
        "它含有多种植物精华，全网销量第一。",
        "使用之后皮肤绝对水润光滑，还能根治痘痘。",
        "现在下单还送小样，想购买的宝子加微信私聊我哦。",
    ],
    "test_video_2": [
        "大家好，今天分享一个赚钱的好机会。",
        "这个项目零风险，稳赚不赔，可以让你轻松躺赚。",
        "每天只要动动手指，就能日赚千元。",
        "好了，今天的分享就到这里，我们下期再见。",
    ],
}

SSML_TMPL = (
    '<speak version="1.0" xml:lang="zh-CN">'
    '<voice name="Microsoft Huihui Desktop">'
    '<prosody rate="-10%">{body}</prosody></voice></speak>'
)


def tts_to_wav(text_lines: list[str], wav_path: Path) -> None:
    body = "".join(
        f"{line}<break time=\"900ms\"/>" for line in text_lines
    )
    ssml = SSML_TMPL.format(body=body)
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.SelectVoice('Microsoft Huihui Desktop');"
        "$s.SetOutputToWaveFile('%s');"
        "$s.SpeakSsml('%s');"
        "$s.Dispose()" % (wav_path, ssml.replace("'", "''"))
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if r.returncode != 0 or not wav_path.exists():
        print("TTS 失败:", r.stderr.decode("utf-8", "ignore"))
        sys.exit(1)
    print(f"  TTS 完成: {wav_path.name} ({wav_path.stat().st_size // 1024} KB)")


def wav_to_mp4(wav_path: Path, mp4_path: Path) -> None:
    import imageio_ffmpeg

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ff, "-y",
        "-f", "lavfi", "-i", "color=c=steelblue:s=640x360:d=3600",
        "-i", str(wav_path),
        "-shortest",
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        str(mp4_path),
    ]
    r = subprocess.run(cmd, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0 or not mp4_path.exists():
        print("ffmpeg 失败:", r.stderr.decode("utf-8", "ignore")[-800:])
        sys.exit(1)
    print(f"  视频完成: {mp4_path.name} ({mp4_path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    for name, lines in SCRIPTS.items():
        print(f"生成 {name}：")
        wav = MEDIA / f"{name}.wav"
        mp4 = MEDIA / f"{name}.mp4"
        tts_to_wav(lines, wav)
        wav_to_mp4(wav, mp4)
    print("\n测试媒体生成完毕 ✓")
