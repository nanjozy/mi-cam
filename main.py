from micam_patch import logging
import os
import time
from asyncio.subprocess import PIPE, create_subprocess_exec
from miloco_sdk import XiaomiClient
from miloco_sdk.cli.utils import get_auth_info, print_device_list
from miot.types import MIoTCameraVideoQuality
from dotenv import load_dotenv
import asyncio

load_dotenv()

RTSP_URL = os.getenv("RTSP_URL", "rtsp://")
DEVICE_NAME = os.getenv("DEVICE_NAME", "")
CODEC = os.getenv("CODEC", "hevc")

INPUT_FPS = os.getenv("INPUT_FPS", "25")


def is_keyframe(data: bytes) -> bool:
    if CODEC == "h264":
        i = 0
        while i < len(data) - 4:
            if (
                data[i] == 0x00
                and data[i + 1] == 0x00
                and (
                    (data[i + 2] == 0x00 and data[i + 3] == 0x01) or data[i + 2] == 0x01
                )
            ):
                nal_unit_type = (
                    (data[i + 3] & 0x1F)
                    if data[i + 2] == 0x01
                    else (data[i + 4] & 0x1F)
                )
                return nal_unit_type == 5
            i += 1
        return False
    elif CODEC == "hevc":
        i = 0
        while i < len(data) - 6:
            if (
                data[i] == 0x00
                and data[i + 1] == 0x00
                and (
                    (data[i + 2] == 0x00 and data[i + 3] == 0x01) or data[i + 2] == 0x01
                )
            ):
                nal_start = i + 3 if data[i + 2] == 0x01 else i + 4
                nal_unit_type = (data[nal_start] >> 1) & 0x3F
                if nal_unit_type in [16, 17, 18, 19, 20]:
                    return True
            i += 1
        return False
    return True


async def run():
    client = XiaomiClient()
    auth_info = get_auth_info(client)
    client.set_access_token(auth_info["access_token"])

    device_list = client.home.get_device_list()
    online_devices = [d for d in device_list if d.get("isOnline", False)]

    device_info = next(
        (d for d in online_devices if d.get("name") == DEVICE_NAME), None
    )
    if not device_info:
        print_device_list()
        logging.warning(f"设备 '{DEVICE_NAME}' 未在线或未找到")
        return False
    logging.info(f"找到设备: {device_info['name']} ({device_info['did']})")
    # 创建音频 FIFO
    audio_fifo = os.path.join("./tmp", "camera_audio.fifo")
    try:
        os.unlink(audio_fifo)
    except FileNotFoundError:
        pass
    os.mkfifo(audio_fifo)

    # 状态
    ffmpeg_proc = None
    audio_file = None
    fifo_ready = asyncio.Event()
    skip_count = 60  # 启动时跳过的帧数以等待关键帧稳定

    async def open_audio_fifo():
        """后台任务：打开音频 FIFO 写端"""
        nonlocal audio_file
        # 这个调用会阻塞直到 ffmpeg 打开读端
        audio_file = await asyncio.to_thread(
            lambda: open(audio_fifo, "wb", buffering=0)
        )
        fifo_ready.set()
        logging.info("音频管道已连接")

    async def on_decode_pcm(did: str, data: bytes, ts: int, channel: int):
        nonlocal fifo_ready
        if not fifo_ready.is_set():
            return

        if audio_file:
            try:
                audio_file.write(data)
                audio_file.flush()
            except BrokenPipeError as e:
                logging.error(f"音频错误 BrokenPipeError: {e}")
            except Exception as e:
                logging.error(f"音频错误: {e}")

    async def on_raw_video(did: str, data: bytes, ts: int, seq: int, channel: int):
        nonlocal ffmpeg_proc, skip_count
        if skip_count > 0:
            skip_count -= 1
            return

        # 等待关键帧并检测 codec
        if ffmpeg_proc is None:
            if is_keyframe(data):
                logging.info("Keyframe detected! Starting stream...")
            else:
                return

            logging.info(f"codec: {CODEC}，FPS: {INPUT_FPS} 启动 ffmpeg...")
            # 启动打开 FIFO 的任务
            asyncio.create_task(open_audio_fifo())

            # 启动 ffmpeg
            ffmpeg_proc = await create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "info",
                "-y",
                "-fflags",
                "+igndts",
                "-f",
                CODEC,
                "-use_wallclock_as_timestamps",
                "1",
                "-probesize",
                "2000000",
                "-analyzeduration",
                "2000000",
                "-i",
                "pipe:0",
                "-use_wallclock_as_timestamps",
                "1",
                "-f",
                "s16le",  # 有符号 16位 小端 PCM
                "-ar",
                "16000",  # 对应解码器的采样率
                "-ac",
                "1",
                "-probesize",
                "2000000",
                "-analyzeduration",
                "2000000",
                "-thread_queue_size",
                "1024",
                "-i",
                audio_fifo,
                # 映射
                "-map",
                "0:v",
                "-map",
                "1:a",
                # 编码
                "-c:v",
                "copy",
                "-bsf:v",
                f"setts=dts=N/{INPUT_FPS}/TB:pts=N/{INPUT_FPS}/TB",
                "-c:a",
                "aac",  # 强制重新编码为 aac 确保兼容性，如果源是 aac 可以 copy
                "-af",
                "aresample=async=1:first_pts=0",
                # 输出
                "-f",
                "rtsp",
                "-rtsp_transport",
                "tcp",
                RTSP_URL,
                stdin=PIPE,
            )
        # 写入视频
        if ffmpeg_proc and ffmpeg_proc.stdin:
            if ffmpeg_proc.stdin.is_closing():
                logging.warning("视频管道已关闭")
                exit(1)
            try:
                ffmpeg_proc.stdin.write(data)
                await ffmpeg_proc.stdin.drain()
            except BrokenPipeError:
                logging.warning("视频 BrokenPipe: FFmpeg 已退出")
                ffmpeg_proc = None
            except Exception as e:
                logging.error(f"视频写入异常: {e}")

    logging.info(f"\n准备推流到: {RTSP_URL}")

    try:
        await client.miot_camera_stream.run_stream(
            device_info["did"],
            0,
            on_raw_video_callback=on_raw_video,
            on_decode_pcm_callback=on_decode_pcm,
            video_quality=MIoTCameraVideoQuality.HIGH,
        )
        await client.miot_camera_stream.wait_for_data()
    except Exception as e:
        logging.error(f"推流失败，请检查设备与当前程序在同一局域网: {e}")
    finally:
        # 关闭音频文件
        if audio_file:
            try:
                audio_file.close()
            except Exception:
                pass

        # 删除 FIFO
        try:
            os.unlink(audio_fifo)
        except Exception:
            pass

        # 关闭 ffmpeg
        if ffmpeg_proc:
            try:
                if ffmpeg_proc.stdin:
                    ffmpeg_proc.stdin.close()
                ffmpeg_proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(run())
