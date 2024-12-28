import asyncio
import signal
from collections import deque
from datetime import timedelta
from io import BytesIO
from os import path
from urllib.parse import quote

import av
import onvif
from custom_pullpoint_manager import (
    CustomPullPointManager,
)  # FIXME: This was modified for Hikvision cameras. It should be generalized.
from telegram import Bot

from user_data import config_data
import logging

RUNLOOP = True
telegram_channel_id = config_data["bots"]["telegram"]["channel_id"]


class VideoStream:
    def __init__(self, bot: Bot, rtsp_url: str):
        """
        Initialize the VideoStream instance.

        :param bot: Telegram Bot instance for sending messages and videos.
        :param rtsp_url: RTSP URL of the video stream.

        A class to handle video streaming from an RTSP source and sending snapshots to a Telegram bot.

        Attributes:
            bot (Bot): The Telegram bot instance.
            rtsp_url (str): The RTSP URL of the video stream.
        """
        self.video_file = BytesIO()
        self.buffer = deque(
            maxlen=250
        )  # FIXME: This value depends on FPS which can vary. It should be calculated dynamically.
        self.ostream = None
        self.codec_name = "hevc"
        self.bot = bot
        self.rtsp_url = rtsp_url

    async def stream_capture(self):
        global RUNLOOP
        while RUNLOOP:
            try:
                rtsp = av.open(
                    self.rtsp_url,
                    options={
                        "rtsp_transport": "tcp",  # Use TCP transport for better reliability
                        "stimeout": "5000000",  # Timeout in microseconds (e.g., 5 seconds)
                    },
                )
                logging.info("RTSP stream opened successfully.")
                for i, stream in enumerate(rtsp.streams):
                    if stream.type == "video":
                        self.codec_name = stream.codec.name
                for i, packet in enumerate(rtsp.demux()):
                    if packet.is_keyframe:
                        await asyncio.sleep(1)
                    if packet.dts is None:
                        continue
                    self.buffer.append(packet)
                    if RUNLOOP == False:
                        break

            except Exception as e:
                logging.warning(
                    f"Error accessing RTSP stream: {e}, retrying in 5 seconds"
                )
                await asyncio.sleep(5)

    async def snapshot(self):
        """
        Capture a snapshot from the video stream and send it to the Telegram bot.

        This method captures a snapshot from the video stream, stores it in a buffer,
        and sends it as a video file to the specified Telegram channel. If the buffer
        is empty, it sends a message indicating that the camera is offline or the RTSP
        stream is not available.
        """
        await asyncio.sleep(6)  # Wait to record post-trigger video
        video_output = av.open(self.video_file, mode="w", format="mp4")
        ostream = video_output.add_stream(codec_name=self.codec_name)
        if self.buffer:
            pts_ref = 0
            first_packet = True
            for packet in self.buffer:
                if first_packet:
                    if packet.is_keyframe:
                        pts_ref = packet.pts
                        packet.pts = 0
                        packet.dts = 0
                        first_packet = False
                    else:
                        continue
                packet.pts -= pts_ref
                packet.dts = packet.pts
                packet.stream = ostream
                video_output.mux(packet)
            video_output.close()
            self.video_file.seek(0)
            await self.bot.send_video(
                chat_id=telegram_channel_id, video=self.video_file
            )

        else:
            await self.bot.send_message(
                chat_id=telegram_channel_id,
                text="Camera is offline or rtsp stream is not available!",
            )


def signal_handler(signal, frame):
    global RUNLOOP
    RUNLOOP = False


signal.signal(signal.SIGINT, signal_handler)


class CameraInstance:
    def __init__(self, bot: Bot, rtsp_stream: VideoStream, camera_id: int):
        """
        Initialize the CameraInstance.

        :param bot: Telegram Bot instance for sending messages.
        :param rtsp_stream: VideoStream instance for handling video streaming.
        :param camera_id: ID of the camera.
        """
        self.bot = bot
        self.rtsp_stream = rtsp_stream
        self.camera_id = camera_id

    def subscription_lost():
        logging.warning("Subscription lost")

    async def run(self):
        """
        Run the camera instance to handle ONVIF events and stream snapshots.

        This method initializes the ONVIF camera, retrieves device information,
        starts the custom pull point manager, and listens for messages. When a
        message indicating motion is received, it captures a snapshot from the
        video stream and sends it to the Telegram bot.
        """
        SUBSCRIPTION_TIME = timedelta(minutes=10)
        WAIT_TIME = timedelta(seconds=30)
        mycam = onvif.ONVIFCamera(
            config_data["cameras"][self.camera_id]["camera_ip"],
            config_data["cameras"][self.camera_id]["camera_onvif_port"],
            config_data["cameras"][self.camera_id]["username"],
            config_data["cameras"][self.camera_id]["password"],
            f"{path.dirname(onvif.__file__)}/wsdl/",
        )
        await mycam.update_xaddrs()
        devicemgmt = await mycam.create_devicemgmt_service()
        device_info = await devicemgmt.GetDeviceInformation()

        logging.info("Camera Model: " + str(device_info["Model"]))

        manager = CustomPullPointManager(
            mycam, SUBSCRIPTION_TIME, self.subscription_lost
        )
        await manager.start()
        await manager.set_synchronization_point()
        global RUNLOOP
        while RUNLOOP:
            pullpoint = manager.get_service()
            logging.info("waiting for messages...")
            try:
                messages = await pullpoint.PullMessages(
                    {
                        "MessageLimit": 100,
                        "Timeout": WAIT_TIME,
                    }
                )
                for cur_message in messages.NotificationMessage:
                    mess_tree = cur_message.Message._value_1.Data.SimpleItem[0].Value
                if mess_tree == "true":
                    await self.rtsp_stream.snapshot()
            except Exception as e:
                logging.info(f"Exception {e} occurred. Retrying..")
        await manager.shutdown()
        await self.bot.send_message(
            chat_id=telegram_channel_id, text="Stopping python node"
        )


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=config_data["bots"]["telegram"]["token"])
    bot_info = await bot.getMe()
    logging.info("Telegram bot name: " + bot_info["username"])
    await bot.send_message(chat_id=telegram_channel_id, text="Starting python node")

    tasks = []
    for id in config_data["cameras"]:
        rtsp_url = f"rtsp://{config_data['cameras'][id]['username']}:{quote(config_data['cameras'][id]['password'])}@{config_data['cameras'][id]['camera_ip']}:554/stream1"
        rtsp_stream = VideoStream(
            bot,
            rtsp_url,
        )
        cam_instance = CameraInstance(bot, rtsp_stream, id)
        tasks.append(asyncio.create_task(rtsp_stream.stream_capture()))
        tasks.append(asyncio.create_task(cam_instance.run()))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
