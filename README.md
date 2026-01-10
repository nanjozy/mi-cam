# Mi CAM


> 本项目是基于 [Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco) 开源框架封装而成的 Python SDK，提供了更便捷的 Python 接口来访问小米智能设备的功能。
> 本项目是基于[miloco-sdk](https://github.com/dairoot/miloco-sdk) fork而来，专门用于摄像头推流。


## 快速开始

### Docker Compose

#### 核显权限
[可选] 获取核显group id。部分摄像头流解码会花屏，需要重新转码。

```
echo "RENDER_GID=$(stat -c %g /dev/dri/renderD128)" >> .env
```
#### docker-compose.yaml
```
services:
  go2rtc:
    container_name: go2rtc
    image: alexxit/go2rtc
    network_mode: host
    restart: always
    environment:
      TZ: Asia/Shanghai
    group_add:
      - "${RENDER_GID}"
    devices:
      - /dev/dri:/dev/dri
    volumes:
      - ./go2rtc:/config
    deploy:
      resources:
        limits:
          memory: 512m
  micam1: &micam
    container_name: micam1
    network_mode: host
    image: nanjozy/micam:latest
    depends_on: [go2rtc]
    environment: &micam_envs
      RTSP_URL: rtsp://192.168.1.xx:8554/a # 填写你在go2rtc中定义的地址
      DEVICE_NAME: "小米智能摄像机2 AI增强版" # 摄像头名称
      TZ: Asia/Shanghai
    volumes:
      - ./micamdata:/app/data # 存放鉴权
      - type: tmpfs # 使用内存作为音频管道
        target: /app/tmp
        tmpfs:
          size: 16000000 # 16MB
    restart: always
    deploy:
     resources:
       limits:
         memory: 512m
  micam2: #可以部署多个摄像头
    <<: *micam
    scale: 1
    container_name: micam2
    group_add: # 添加核显权限
      - "${RENDER_GID}"
    devices:
      - /dev/dri:/dev/dri
    environment:
      <<: *micam_envs
      RTSP_URL: rtsp://192.168.1.xx:8554/b # 填写你在go2rtc中定义的地址
      DEVICE_NAME: "书房的摄像头"  # 摄像头名称
      CODEC_FIX: "1"  # 开启转码
      HWACC: "1"  # 开启intel核显加速
      LIBVA_DRIVER_NAME: iHD
```
#### Go2rtc
1. Open Go2rtc WebUI / 访问Go2rtc网页: http://192.168.1.xx:1984/config.html
2. Config empty streams / 配置空视频流:
```
streams:
  a:
  b:
```
3. Save & Restart / 保存并重启

## 许可证

本项目基于 [Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco) 开源框架开发，因此必须遵守 [Xiaomi Miloco License Agreement](https://github.com/XiaoMi/xiaomi-miloco/blob/main/LICENSE.md)。



## 致谢

感谢 [Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco) 项目团队提供的优秀开源框架。
