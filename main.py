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
    """
    start = 0
    max_len = len(data)

    while start < max_len:
        pos = data.find(b"\x00\x00\x01", start)
        if pos == -1:
            break

        header_pos = pos + 3
        if header_pos >= max_len:
            break

        header = data[header_pos]
        start = header_pos + 1

        if header & 0x80 != 0:
            continue

        h264_type = header & 0x1F
        h265_type = (header >> 1) & 0x3F

        if h265_type == 32:
            return True, "hevc"

        if h264_type in (7, 8):
            return True, "h264"

        if h265_type in (33, 34):
            return True, "hevc"

        if h265_type in (19, 20):
            return True, "hevc"

        if h264_type == 5:
            return True, "h264"

    return False, "unknown"


async def stream_task():
    """
    单次推流任务逻辑。
    如果发生错误或 ffmpeg 退出，将抛出异常或返回，由外层循环负责重启。
    """
    logger.info(f"目标 RTSP 地址: {RTSP_URL} 设备名称: {DEVICE_NAME}")
    client = XiaomiClient()
    auth_info = get_auth_info(client)
    client.set_access_token(auth_info["access_token"])

    device_list = client.home.get_device_list()
    online_devices = [d for d in device_list if d.get("isOnline", False)]

    if not online_devices:
        logger.error("设备列表: 暂无在线设备")
        return False  # 返回 False 表示非网络错误的逻辑终止，但也需要重试

    device_info = None
    for d in online_devices:
        if d.get("name") == DEVICE_NAME:
            device_info = d
            break
    if not device_info:
        print_device_list(online_devices)
        logger.error(f"设备列表: 未找到名称为 '{DEVICE_NAME}' 的在线设备")
        return False

    logger.info(f"选择设备: {device_info['name']}: ({device_info['did']})")

    # 创建音频 FIFO
    audio_fifo = os.path.join("tmp", "camera_audio.fifo")
    if not os.path.exists("tmp"):
        os.makedirs("tmp", exist_ok=True)

    try:
        if os.path.exists(audio_fifo):
            os.unlink(audio_fifo)
        os.mkfifo(audio_fifo)
    except OSError as e:
        logger.exception(f"创建 FIFO 失败: {e}")
        return False

    # 状态变量
    ffmpeg_proc = None
    codec = None
    frame_count = 0
    audio_frame_count = 0
    audio_file = None
    fifo_ready = asyncio.Event()
    stop_event = asyncio.Event()
    video_queue = asyncio.Queue(maxsize=5)
    writer_task = None

    async def open_audio_fifo():
        nonlocal audio_file
        try:
            audio_file = await asyncio.to_thread(
                lambda: open(audio_fifo, "wb", buffering=0)
            )
            fifo_ready.set()
            logger.info("音频管道已连接")
        except Exception as e:
            logger.exception(f"打开音频管道失败: {e}")
            stop_event.set()

    async def ffmpeg_writer_worker():
        """专门负责写入 FFmpeg 的消费者任务"""
        while not stop_event.is_set():
            try:
                # 获取数据
                data = await video_queue.get()

                if ffmpeg_proc and ffmpeg_proc.stdin:
                    try:
                        ffmpeg_proc.stdin.write(data)
                        # === 核心修改：每帧都立即 flush，禁止 Python 层面缓冲 ===
                        await ffmpeg_proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        logger.exception("FFmpeg 管道断裂 (推流中断)")
                        stop_event.set()
                    except Exception as e:
                        logger.exception(f"写入 FFmpeg 异常: {e}")

                video_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Writer Worker 未知异常: {e}")
                break

    async def on_decode_pcm(did: str, data: bytes, ts: int, channel: int):
        nonlocal audio_frame_count
        audio_frame_count += 1

        if not fifo_ready.is_set():
            return

        # 如果已经触发停止，不再处理
        if stop_event.is_set():
            return

        if audio_file:
            try:
                audio_file.write(data)
            except (BrokenPipeError, OSError):
                # 音频管道断裂通常意味着 ffmpeg 挂了
                if not stop_event.is_set():
                    logger.warning("音频写入 BrokenPipe，触发重启...")
                    stop_event.set()
            except Exception as e:
                logger.exception(f"音频错误: {e}")

    async def on_raw_video(did: str, data: bytes, ts: int, seq: int, channel: int):
        nonlocal ffmpeg_proc, codec, frame_count, writer_task
        frame_count += 1

        if stop_event.is_set():
            return

        # 等待关键帧并检测 codec
        if ffmpeg_proc is None:
            is_keyframe, detected = detect_keyframe_and_codec(data)
            if not is_keyframe or detected == "unknown":
                if frame_count % 50 == 0:
                    logger.info(f"等待关键帧... 第 {frame_count} 帧")
                return

            codec = detected
            logger.info(f"检测到 codec: {codec}，启动 ffmpeg...")

            asyncio.create_task(open_audio_fifo())

            common_flags = [
                "-y",
                "-v",
                "error",
                "-hide_banner",
                "-thread_queue_size",
                "512",
                "-f",
                codec,
                "-i",
                "pipe:0",
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
            ]

            output_flags = [
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-ar",
                "16000",
                "-af",
                "aresample=async=1:first_pts=0",
                "-f",
                "rtsp",
                "-rtsp_transport",
                "tcp",
                "-max_delay",
                "0",
                RTSP_URL,
            ]

            try:
                if not CODEC_FIX and not HWACC:
                    # 默认模式：Copy Video
                    cmd = (
                        ["ffmpeg"]
                        + [
                            "-use_wallclock_as_timestamps",
                            "1",
                            "-probesize",
                            "32",
                            "-analyzeduration",
                            "0",
                            "-fflags",
                            "+genpts+nobuffer+discardcorrupt",
                            "-flags",
                            "low_delay",
                        ]
                        + common_flags
                        + ["-use_wallclock_as_timestamps", "1"]
                        + ["-c:v", "copy"]
                        + output_flags
                    )

                elif HWACC:
                    # VAAPI 硬件加速
                    cmd = (
                        ["ffmpeg"]
                        + [
                            "-init_hw_device",
                            "vaapi=va:/dev/dri/renderD128",
                            "-filter_hw_device",
                            "va",
                            "-fflags",
                            "+genpts+nobuffer",
                            "-err_detect",
                            "ignore_err",
                            "-analyzeduration",
                            "50000",
                            "-probesize",
                            "50000",
                            "-hwaccel",
                            "vaapi",
                            "-hwaccel_output_format",
                            "vaapi",
                            "-hwaccel_device",
                            "va",
                        ]
                        + common_flags
                        + [
                            "-vf",
                            "scale_vaapi=format=nv12",
                            "-c:v",
                            "hevc_vaapi",
                            "-qp",
                            "25",
                            "-bf",
                            "0",
                            "-g",
                            "30",
                        ]
                        + output_flags
                    )
                else:
                    # CPU 转码模式
                    cmd = (
                        ["ffmpeg"]
                        + [
                            "-fflags",
                            "+genpts+nobuffer",
                            "-err_detect",
                            "ignore_err",
                            "-analyzeduration",
                            "50000",
                            "-probesize",
                            "50000",
                        ]
                        + common_flags
                        + [
                            "-c:v",
                            "libx264",
                            "-preset",
                            "ultrafast",
                            "-tune",
                            "zerolatency",
                            "-g",
                            "60",
                        ]
                        + output_flags
                    )

                ffmpeg_proc = await create_subprocess_exec(*cmd, stdin=PIPE)

                # 启动写入消费者
                writer_task = asyncio.create_task(ffmpeg_writer_worker())

                # 将当前这一帧（关键帧）放入队列
                await video_queue.put(data)

            except Exception as e:
                logger.exception(f"启动 FFmpeg 失败: {e}")
                stop_event.set()
                return
        # 当 FFmpeg 启动后，使用队列逻辑：
        else:
            # 检查 ffmpeg 是否存活
            if ffmpeg_proc.returncode is not None:
                stop_event.set()
                return

            # === 核心修改：漏桶策略（丢弃旧帧）===
            if video_queue.full():
                try:
                    # 扔掉最旧的一帧
                    _ = video_queue.get_nowait()
                    video_queue.task_done()
                    # logger.debug("Drop Frame") # 调试时可开启
                except asyncio.QueueEmpty:
                    pass

            try:
                video_queue.put_nowait(data)
            except Exception:
                pass

    logger.info(f"准备推流到: {RTSP_URL}")

    try:
        # 启动 SDK 的推流任务
        await client.miot_camera_stream.run_stream(
            device_info["did"],
            0,
            on_raw_video_callback=on_raw_video,
            on_decode_pcm_callback=on_decode_pcm,
            video_quality=MIoTCameraVideoQuality.HIGH,
        )

        # 我们需要同时等待：
        # 1. SDK 的 wait_for_data (正常流程)
        # 2. stop_event (FFmpeg 报错触发的异常流程)
        wait_list = [
            asyncio.create_task(client.miot_camera_stream.wait_for_data()),
            asyncio.create_task(stop_event.wait()),
        ]

        # 如果 writer 已经启动，也要监控它是否报错退出
        if writer_task:
            wait_list.append(writer_task)

        done, pending = await asyncio.wait(
            wait_list, return_when=asyncio.FIRST_COMPLETED
        )

        if stop_event.is_set():
            logger.warning("检测到停止信号，重置任务...")

        for task in pending:
            task.cancel()

    except Exception as e:
        logger.exception(f"推流主逻辑发生错误: {e}")
    finally:
        # === 资源清理 ===
        logger.info("清理资源...")
        # 停止写入任务
        if writer_task:
            writer_task.cancel()
            try:
                await writer_task
            except asyncio.CancelledError:
                pass
        # 1. 关闭音频文件句柄
        if audio_file:
            try:
                audio_file.close()
            except Exception:
                pass

        # 2. 删除管道文件
        try:
            if os.path.exists(audio_fifo):
                os.unlink(audio_fifo)
        except Exception:
            pass

        # 3. 强杀 ffmpeg
        if ffmpeg_proc:
            try:
                if ffmpeg_proc.stdin:
                    try:
                        ffmpeg_proc.stdin.close()
                    except Exception:
                        pass
                # 尝试优雅退出
                ffmpeg_proc.terminate()
                try:
                    await asyncio.wait_for(ffmpeg_proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning("FFmpeg 未响应，强制 Kill")
                    ffmpeg_proc.kill()
            except Exception as e:
                logger.exception(f"关闭 FFmpeg 异常: {e}")

    logger.info("本轮推流结束")
    return True  # 返回 True 表示应该重试


async def main_supervisor():
    """
    守护进程循环：无限重试
    """
    retry_delay = 5
    while True:
        try:
            logger.info(">>> 开始新一轮推流任务 <<<")
            should_retry = await stream_task()

            if not should_retry:
                # 如果是因为找不到设备等逻辑错误，稍微多睡一会再试
                retry_delay = 30
            else:
                retry_delay = 5

        except Exception as e:
            logger.critical(f"守护进程捕获到未处理异常: {e}")

        logger.info(f"等待 {retry_delay} 秒后重试...")
        await asyncio.sleep(retry_delay)


if __name__ == "__main__":
    try:
        asyncio.run(main_supervisor())
    except KeyboardInterrupt:
        logger.info("用户停止脚本")
