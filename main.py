import micam_patch as _
import os
from asyncio.subprocess import PIPE, create_subprocess_exec
from loguru import logger
from miloco_sdk import XiaomiClient
from miloco_sdk.cli.utils import get_auth_info, print_device_list
from miot.types import MIoTCameraVideoQuality
from dotenv import load_dotenv
import asyncio

load_dotenv()
# RTSP 服务器地址
RTSP_URL = os.getenv("RTSP_URL", "rtsp://")
DEVICE_NAME = os.getenv("DEVICE_NAME", "")
CODEC_FIX = os.getenv("CODEC_FIX", "0") == "1"
HWACC = os.getenv("HWACC", "0") == "1"


def detect_keyframe_and_codec(data: bytes) -> tuple[bool, str]:
    """
    检测关键帧和 codec 类型。
    注意：单帧数据极难 100% 准确区分 H.264/H.265，因为 NALU type 存在数值重叠。
    此函数采用优先匹配确信度高的类型策略。
    """
    start = 0
    max_len = len(data)

    # 只要还能找到起始码前缀
    while start < max_len:
        # 1. 快速查找起始码 (00 00 01)
        # H.264/H.265 起始码可能是 00 00 01 或 00 00 00 01
        # find 只能找固定的，我们找 00 00 01，它能兼容两种情况
        pos = data.find(b"\x00\x00\x01", start)
        if pos == -1:
            break

        # 确定 NAL Unit Header 的位置
        # pos 是 00 00 01 的位置，header 在 pos + 3
        header_pos = pos + 3

        # 越界检查
        if header_pos >= max_len:
            break

        header = data[header_pos]

        # 移动 start 到下一次查找的位置 (避免死循环)
        start = header_pos + 1

        # 2. 校验 Forbidden Zero Bit (Bit 0 必须为 0)
        # 如果是 1，说明可能找错了位置，或者是坏数据
        if header & 0x80 != 0:
            continue

        # 3. 解析类型
        # H.264: 后 5 位
        h264_type = header & 0x1F
        # H.265: 中间 6 位
        h265_type = (header >> 1) & 0x3F

        # --- 判定逻辑 (优先级非常重要) ---

        # [HEVC] 强特征：VPS (32)
        # H.264 没有 type 32 (5 bit 最大 31)，所以如果算出 32，必是 HEVC
        if h265_type == 32:
            return True, "hevc"

        # [H.264] 强特征：SPS (7), PPS (8)
        # 需要排除这些数值在 HEVC 下被误判为特殊帧的可能
        # H.264 SPS(7) -> hex 07/27/47/67...
        #   0x67 (0110 0111) -> HEVC: (0x67>>1)&0x3F = 51 (未知/保留) -> 安全
        #   0x27 (0010 0111) -> HEVC: 19 (IDR) -> **冲突风险**
        # 但通常 SPS/PPS 会伴随 IDR 出现，我们优先返回 SPS/PPS 判定
        if h264_type in (7, 8):
            return True, "h264"

        # [HEVC] SPS (33), PPS (34)
        if h265_type in (33, 34):
            return True, "hevc"

        # [HEVC] IDR (19, 20)
        if h265_type in (19, 20):
            # 这里存在严重歧义，H.264 的某些非关键帧字节可能算出 19/20
            # 仅凭一个字节很难断定，但如果确实需要返回，倾向于认为是 HEVC IDR
            return True, "hevc"

        # [H.264] IDR (5)
        if h264_type == 5:
            return True, "h264"

    # 如果遍历完所有 NALU 都没找到关键帧特征
    return False, "unknown"


async def run():
    logger.info(f"目标 RTSP 地址: {RTSP_URL} 设备名称: {DEVICE_NAME}")
    client = XiaomiClient()
    auth_info = get_auth_info(client)
    client.set_access_token(auth_info["access_token"])

    device_list = client.home.get_device_list()
    online_devices = [d for d in device_list if d.get("isOnline", False)]

    if not online_devices:
        logger.error("\n设备列表: 暂无在线设备")
        return
    device_info = None
    for d in online_devices:
        if d.get("name") == DEVICE_NAME:
            device_info = d
            break
    if not device_info:
        print_device_list(online_devices)
        logger.error(f"\n设备列表: 未找到名称为 '{DEVICE_NAME}' 的在线设备")
        return

    logger.info(f"选择设备: {device_info['name']}: ({device_info['did']})")

    # 创建音频 FIFO
    audio_fifo = os.path.join("tmp", "camera_audio.fifo")
    try:
        os.unlink(audio_fifo)
    except FileNotFoundError:
        pass
    os.mkfifo(audio_fifo)

    # 状态
    ffmpeg_proc = None
    codec = None
    frame_count = 0
    audio_frame_count = 0
    audio_file = None
    fifo_ready = asyncio.Event()

    async def open_audio_fifo():
        """后台任务：打开音频 FIFO 写端"""
        nonlocal audio_file
        loop = asyncio.get_event_loop()
        # 这个调用会阻塞直到 ffmpeg 打开读端
        audio_file = await loop.run_in_executor(
            None, lambda: open(audio_fifo, "wb", buffering=0)
        )
        fifo_ready.set()
        logger.info("音频管道已连接")

    async def on_decode_pcm(did: str, data: bytes, ts: int, channel: int):
        """接收解码后的 PCM 音频数据"""
        nonlocal audio_frame_count
        audio_frame_count += 1

        # 等待 FIFO 就绪
        if not fifo_ready.is_set():
            return

        if audio_file:
            try:
                audio_file.write(data)
                # if audio_frame_count % 200 == 0:
                #     logger.debug(f"音频推流中... 第 {audio_frame_count} 帧")
            except BrokenPipeError:
                pass
            except Exception as e:
                logger.error(f"音频错误: {e}")

    async def on_raw_video(did: str, data: bytes, ts: int, seq: int, channel: int):
        nonlocal ffmpeg_proc, codec, frame_count
        frame_count += 1

        # 等待关键帧并检测 codec
        if ffmpeg_proc is None:
            is_keyframe, detected = detect_keyframe_and_codec(data)
            if not is_keyframe or detected == "unknown":
                if frame_count % 50 == 0:
                    logger.info(f"等待关键帧... 第 {frame_count} 帧")
                return

            codec = detected
            logger.info(f"检测到 codec: {codec}，启动 ffmpeg...")

            # 启动打开 FIFO 的任务
            asyncio.create_task(open_audio_fifo())
            if not CODEC_FIX:
                # 启动 ffmpeg
                ffmpeg_proc = await create_subprocess_exec(
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-hide_banner",
                    "-fflags",
                    "nobuffer",  # 关键：减少输入缓冲，降低延迟
                    "-flags",
                    "low_delay",  # 告诉解码器/解复用器这是一个低延迟流
                    # 视频输入 - 使用系统时钟作为时间戳
                    "-use_wallclock_as_timestamps",
                    "1",
                    "-analyzeduration",
                    "10000",  # 20 seconds
                    "-probesize",
                    "10000",  # 20 MB
                    "-thread_queue_size",
                    "512",
                    "-fflags",
                    "+genpts",
                    "-f",
                    codec,
                    "-i",
                    "pipe:0",
                    # 音频输入 - 同样使用系统时钟
                    "-use_wallclock_as_timestamps",
                    "1",
                    "-thread_queue_size",
                    "512",
                    "-f",
                    "s16le",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
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
                    # "-bsf:v", "hevc_mp4toannexb",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "64k",
                    "-ar",
                    "16000",
                    # 音频时间戳修复
                    "-af",
                    "aresample=async=1000",
                    # "aresample=async=1:first_pts=0",
                    # 输出
                    "-f",
                    "rtsp",
                    "-rtsp_transport",
                    "tcp",
                    "-max_delay",
                    "100000",
                    RTSP_URL,
                    stdin=PIPE,
                )
            elif HWACC:
                ffmpeg_proc = await create_subprocess_exec(
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-hide_banner",
                    "-init_hw_device",
                    "vaapi=va:/dev/dri/renderD128",
                    "-filter_hw_device",
                    "va",
                    # ---------------------------
                    "-fflags",
                    "+genpts+nobuffer+discardcorrupt",
                    "-err_detect",
                    "ignore_err",
                    "-analyzeduration",
                    "100000",
                    "-probesize",
                    "100000",
                    "-thread_queue_size",
                    "512",
                    "-hwaccel",
                    "vaapi",
                    "-hwaccel_output_format",
                    "vaapi",
                    "-hwaccel_device",
                    "va",
                    "-f",
                    codec,
                    "-i",
                    "pipe:0",
                    # 音频输入
                    # "-use_wallclock_as_timestamps",
                    # "1",
                    "-thread_queue_size",
                    "512",
                    "-f",
                    "s16le",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-i",
                    audio_fifo,
                    # 映射
                    "-map",
                    "0:v",
                    "-map",
                    "1:a",
                    "-vf",
                    "scale_vaapi=format=nv12",
                    "-c:v",
                    "hevc_vaapi",
                    "-qp",
                    "25",
                    "-bf",
                    "0",  # [关键] 输出端禁用 B 帧，实现真正的低延迟
                    "-g",
                    "30",  # GOP 设为 30 (1秒一个I帧)，加快客户端首屏加载速度
                    # -----------------------------
                    "-c:a",
                    "aac",
                    "-b:a",
                    "64k",
                    "-ar",
                    "16000",
                    # 音频时间戳修复
                    "-af",
                    "aresample=async=1000",
                    # 输出
                    "-f",
                    "rtsp",
                    "-rtsp_transport",
                    "tcp",
                    "-max_delay",
                    "100000",
                    RTSP_URL,
                    stdin=PIPE,
                )
            else:
                ffmpeg_proc = await create_subprocess_exec(
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-hide_banner",
                    "-fflags",
                    "+genpts+nobuffer+discardcorrupt",
                    "-err_detect",
                    "ignore_err",
                    "-analyzeduration",
                    "100000",
                    "-probesize",
                    "100000",
                    "-thread_queue_size",
                    "512",
                    "-f",
                    codec,
                    "-i",
                    "pipe:0",
                    # 音频输入
                    # "-use_wallclock_as_timestamps",
                    # "1",
                    "-thread_queue_size",
                    "512",
                    "-f",
                    "s16le",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-i",
                    audio_fifo,
                    # 映射
                    "-map",
                    "0:v",
                    "-map",
                    "1:a",
                    "-c:v",
                    "libx264",  # 强制转码为 H264
                    "-preset",
                    "ultrafast",  # 极速模式，降低 CPU 占用
                    "-tune",
                    "zerolatency",  # 零延迟调优
                    "-g",
                    "60",  # 关键帧间隔 (2秒)
                    # -----------------------------
                    "-c:a",
                    "aac",
                    "-b:a",
                    "64k",
                    "-ar",
                    "16000",
                    # 音频时间戳修复
                    "-af",
                    "aresample=async=1000",
                    # 输出
                    "-f",
                    "rtsp",
                    "-rtsp_transport",
                    "tcp",
                    "-max_delay",
                    "100000",
                    RTSP_URL,
                    stdin=PIPE,
                )

        # 写入视频
        if ffmpeg_proc and ffmpeg_proc.stdin and not ffmpeg_proc.stdin.is_closing():
            try:
                ffmpeg_proc.stdin.write(data)
                # if frame_count % 100 == 0:
                #     logger.debug(
                #         f"视频推流中... 第 {frame_count} 帧, 音频 {audio_frame_count} 帧"
                #     )
            except Exception:
                pass

    logger.info(f"\n准备推流到: {RTSP_URL}")

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
        logger.error(f"推流失败，请检查设备与当前程序在同一局域网: {e}")
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
