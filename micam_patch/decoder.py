from miot.decoder import (
    MIoTMediaDecoder,
    MIoTCameraFrameData,
    MIoTCameraCodec,
    AudioCodecContext,
    AudioResampler,
    _LOGGER,
    Packet,
)
from loguru import logger
from .audiocodec import G711ADecoder


def _on_audio_callback(self, frame_data: MIoTCameraFrameData) -> None:
    if not self._audio_decoder:
        # Create audio decoder
        if frame_data.codec_id == MIoTCameraCodec.AUDIO_OPUS:
            self._audio_decoder = AudioCodecContext.create("opus", "r")
        elif frame_data.codec_id == MIoTCameraCodec.AUDIO_G711A:
            self._audio_decoder = G711ADecoder(sample_rate=16000, channels=0)
        else:
            logger.warning("frame_data", frame_data)
        self._resampler = AudioResampler(format="s16", layout="mono", rate=16000)
        _LOGGER.info("audio decoder created, %s", frame_data.codec_id)
    pkt = Packet(frame_data.data)
    frames: List[AudioFrame] = self._audio_decoder.decode(pkt)  # type: ignore
    pcm_bytes: bytes = b""
    for frame in frames:
        rs_frames = self._resampler.resample(frame)
        for rs_frame in rs_frames:
            pcm_bytes += rs_frame.to_ndarray().tobytes()
    self._main_loop.call_soon_threadsafe(
        self._main_loop.create_task,
        self._audio_callback(pcm_bytes, frame_data.timestamp, frame_data.channel),
    )


MIoTMediaDecoder._on_audio_callback = _on_audio_callback
