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
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from user_data import config_data
import logging

RUNLOOP = True


class BotHandler:
    def __init__(self, token: str, telegram_channel_id: int):
        self.rtsp_stream = None
        self.token = token
        self.telegram_channel_id = telegram_channel_id
        self.telegram_bot = Application.builder().token(token).build()
        self.telegram_bot.add_handler(
            CommandHandler("grabimage", self.grabimage))
        self.telegram_bot.add_handler(
            CommandHandler("grabvideo", self.grabvideo))

    async def grabimage(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if self.rtsp_stream is not None:
            await self.rtsp_stream.image_snapshot()

    async def grabvideo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if self.rtsp_stream is not None:
            await self.rtsp_stream.video_snapshot()

    async def send_message(self, text: str):
        bot = self.telegram_bot.bot
        await bot.send_message(chat_id=self.telegram_channel_id, text=text)

    async def send_video(self, video: BytesIO):
        bot = self.telegram_bot.bot
        await bot.send_video(chat_id=self.telegram_channel_id, video=video, write_timeout=100)

    async def send_photo(self, photo: BytesIO):
        bot = self.telegram_bot.bot
        await bot.send_photo(chat_id=self.telegram_channel_id, photo=photo)

    async def run(self):
        bot = self.telegram_bot.bot
        bot_info = await bot.get_me()
        logging.info("Telegram bot name: " + bot_info["username"])
        await bot.send_message(
            chat_id=self.telegram_channel_id, text="Starting python node"
        )

        await self.telegram_bot.initialize()
        await self.telegram_bot.start()
        await self.telegram_bot.updater.start_polling()

    async def stop(self):
        await self.telegram_bot.stop()


class VideoStream:
    def __init__(self, bot: BotHandler, rtsp_url: str):
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
            # FIXME: This value depends on FPS which can vary. It should be calculated dynamically.
        )
        self.latest_keyframe = None
        self.ostream = None
        self.codec_name = "hevc"
        self.bot = bot
        self.rtsp_url = rtsp_url

    async def stream_capture(self):
        while RUNLOOP:
            try:
                rtsp = av.open(
                    self.rtsp_url,
                    options={
                        "rtsp_transport": "tcp",  # Use TCP transport for better reliability
                        # Timeout in microseconds (e.g., 5 seconds)
                        "stimeout": "5000000",
                    },
                )
                logging.info("RTSP stream opened successfully.")
                for _, stream in enumerate(rtsp.streams):
                    if stream.type == "video":
                        self.codec_name = stream.codec.name
                for _, packet in enumerate(rtsp.demux()):
                    if packet.is_keyframe:
                        self.latest_keyframe = packet
                        await asyncio.sleep(1)
                    if packet.dts is None:
                        continue
                    self.buffer.append(packet)
                    if RUNLOOP is False:
                        break

            except Exception as e:
                logging.warning(
                    f"Error accessing RTSP stream: {e}, retrying in 5 seconds"
                )
                await asyncio.sleep(5)

    async def image_snapshot(self):
        if self.latest_keyframe is not None:
            image_file = BytesIO()
            for frame in self.latest_keyframe.decode():
                if frame.key_frame:
                    frame.to_image().save(image_file, format="JPEG")
                    image_file.seek(0)
                    await self.bot.send_photo(photo=image_file)
                    break

    async def video_snapshot(self):
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
            await self.bot.send_video(video=self.video_file)

        else:
            await self.bot.send_message(
                text="Camera is offline or rtsp stream is not available!",
            )


def signal_handler(signal, frame):
    global RUNLOOP
    RUNLOOP = False


signal.signal(signal.SIGINT, signal_handler)


class CameraInstance:
    def __init__(self, bot: BotHandler, rtsp_stream: VideoStream, camera_id: int):
        """
        Initialize the CameraInstance.

        :param bot: Telegram Bot instance for sending messages.
        :param rtsp_stream: VideoStream instance for handling video streaming.
        :param camera_id: ID of the camera.
        """
        self.bot = bot
        self.rtsp_stream = rtsp_stream
        self.camera_id = camera_id

    def subscription_lost(self):
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
                    await self.rtsp_stream.video_snapshot()
            except Exception as e:
                logging.info(f"Exception {e} occurred. Retrying..")
        await manager.shutdown()
        await self.bot.send_message(text="Stopping python node")


async def main():
    """
    Main function to initialize and run the bot and camera instances.

    This function sets up logging, creates an instance of the BotHandler,
    and initializes tasks for running the bot and handling video streams
    for each camera defined in the configuration.

    :return: None
    """
    logging.basicConfig(level=logging.INFO)
    bot_instance = BotHandler(
        config_data["bots"]["telegram"]["token"],
        config_data["bots"]["telegram"]["channel_id"],
    )
    tasks = []
    tasks.append(asyncio.create_task(bot_instance.run()))
    for camera_name, camera_config in config_data["cameras"].items():
        rtsp_url = f"rtsp://{camera_config['username']}:{quote(camera_config['password'])}@{camera_config['camera_ip']}:554/stream1"
        rtsp_stream = VideoStream(
            bot_instance,
            rtsp_url,
        )
        cam_instance = CameraInstance(bot_instance, rtsp_stream, camera_name)
        # FIXME: If multiple cameras are used, this should be changed.
        bot_instance.rtsp_stream = rtsp_stream
        tasks.append(asyncio.create_task(rtsp_stream.stream_capture()))
        tasks.append(asyncio.create_task(cam_instance.run()))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
