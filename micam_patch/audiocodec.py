import audioop
import av
import numpy as np

class G711ADecoder:
    """
    模拟 PyAV 的解码器接口，专门处理 G.711A (pcm_alaw)
    """
    def __init__(self, sample_rate=8000, channels=1):
        self.sample_rate = sample_rate
        self.channels = channels

    def decode(self, packet):
        # packet 可能是 av.Packet 或 bytes，这里做兼容处理
        data = bytes(packet)
        
        # 核心逻辑：使用 audioop 将 alaw 转为 16-bit Linear PCM
        # width=2 代表目标是 16位 (2字节)
        pcm_data = audioop.alaw2lin(data, 2)
        
        # 如果你的后续代码需要 av.AudioFrame 对象：
        # 我们手动构建一个 AudioFrame 返回去
        # 注意：G.711A 通常是单声道 (mono)，数据需要 reshape 为 (1, N)
        array = np.frombuffer(pcm_data, dtype=np.int16).reshape(1, -1)
        
        frame = av.AudioFrame.from_ndarray(array, format='s16', layout='mono')
        frame.sample_rate = self.sample_rate
        # frame.pts = packet.pts # 如果需要保留时间戳，可以在这里赋值
        
        return [frame] # PyAV 的 decode 通常返回帧列表