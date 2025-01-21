import asyncio
import signal
from queue import Queue
from datetime import timedelta
import time
from io import BytesIO
from os import path
from urllib.parse import quote
import threading
import av
import onvif
from custom_pullpoint_manager import (
    CustomPullPointManager,
)  # FIXME: This was modified for Hikvision cameras. It should be generalized.
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from user_data import config_data
import logging
from slack_sdk.web.async_client import AsyncWebClient

RUNLOOP = True


class BotHandler:
    def __init__(self, token: str, channel_id: str):
        self.token = token
        self.channel_id = channel_id
        self.rtsp_streams = []

    async def send_message(self, text: str):
        pass

    async def send_video(self, video: BytesIO):
        pass

    async def send_photo(self, photo: BytesIO):
        pass

    async def run(self):
        pass

    async def stop(self):
        pass


class SlackBot(BotHandler):
    def __init__(self, token: str, slack_channel_id: str):
        self.token = token
        self.slack_channel_id = slack_channel_id
        self.rtsp_streams = []
        self.slack_bot = AsyncWebClient(token=token)

    async def send_message(self, text: str):
        await self.slack_bot.chat_postMessage(channel=self.slack_channel_id, text=text)

    async def send_video(self, video: BytesIO):
        await self.upload_file(file=video, title="Video")

    async def send_photo(self, photo: BytesIO):
        await self.upload_file(file=photo, title="Photo")

    async def upload_file(self, file: BytesIO, title: str):
        await self.slack_bot.files_upload_v2(channel=self.slack_channel_id, file=file, title=title)

    async def run(self):
        bot_name = await self.slack_bot.auth_test()
        logging.info("Slack bot name: " + bot_name["user"])
        await self.slack_bot.chat_postMessage(channel=self.slack_channel_id, text="Starting python node")


class TelegramBot(BotHandler):
    def __init__(self, token: str, telegram_channel_id: int):
        self.rtsp_stream = None
        self.token = token
        self.telegram_channel_id = telegram_channel_id
        self.rtsp_streams = []
        self.telegram_bot = Application.builder().token(token).build()
        self.telegram_bot.add_handler(
            CommandHandler("grabimage", self.grabimage))
        self.telegram_bot.add_handler(
            CommandHandler("grabvideo", self.grabvideo))

    async def grabimage(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        for stream in self.rtsp_streams:
            image = stream.image_snapshot()
            if image is not None:
                await self.send_photo(image)

    async def grabvideo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        for stream in self.rtsp_streams:
            video = await stream.video_snapshot()
            if video is not None:
                await self.send_video(video)

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
    def __init__(self, rtsp_url: str):
        """
        Initialize the VideoStream instance.

        :param bot: Telegram Bot instance for sending messages and videos.
        :param rtsp_url: RTSP URL of the video stream.

        A class to handle video streaming from an RTSP source and sending snapshots to a Telegram bot.

        Attributes:
            bot (Bot): The Telegram bot instance.
            rtsp_url (str): The RTSP URL of the video stream.
        """
        self.buffer = Queue[av.Packet]()
        self.latest_keyframe = None
        self.ostream = None
        self.in_stream = None
        self.rtsp_url = rtsp_url
        self.video_in_progress = False
        self.video_thread = threading.Thread(
            target=self.stream_capture).start()

    def stream_capture(self):
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
                fps = rtsp.streams.video[0].average_rate
                queue_size = 200  # Default queue size
                if fps > 0 and fps < 40:
                    # Keep the last 5 seconds of video
                    queue_size = int(fps * 5)
                self.in_stream = rtsp.streams.video[0]
                for packet in rtsp.demux(self.in_stream):
                    while self.buffer.qsize() >= queue_size and self.video_in_progress is False:
                        self.buffer.get()
                    if packet.dts is None:
                        continue
                    if packet.is_keyframe:
                        self.latest_keyframe = packet
                    self.buffer.put(packet)
                    if RUNLOOP is False:
                        break

            except Exception as e:
                logging.warning(
                    f"Error accessing RTSP stream: {e}, retrying in 5 seconds"
                )
                time.sleep(5)

    def image_snapshot(self):
        if self.latest_keyframe is not None:
            image_file = BytesIO()
            for frame in self.latest_keyframe.decode():
                if frame.key_frame:
                    frame.to_image().save(image_file, format="JPEG")
                    image_file.seek(0)
                    return image_file
        return None

    async def video_snapshot(self):
        """
        Captures a video snapshot from the current video stream buffer.

        This method captures a video snapshot from the current video stream 
        buffer and returns it as a BytesIO object containing the video in MP4 
        format. If a video capture is already in progress, the method will 
        return immediately without starting a new capture.

        Returns:
            BytesIO: A BytesIO object containing the captured video in MP4 
            format, or None if no video was captured.

        Raises:
            Exception: If an error occurs during the muxing of video packets.

        Notes:
            - The method waits for 10 seconds before starting the video capture.
            - The video capture process is skipped if there is no keyframe in 
            the buffer.
            - The method sets `self.video_in_progress` to True at the start and 
            False at the end to prevent concurrent captures.
        """
        if self.video_in_progress is True:
            return  # Do not start a new video capture if one is already in progress
        # FIXME: Wait to record post-trigger video, should be calculated dynamically from FPS
        self.video_in_progress = True
        await asyncio.sleep(10)
        video_file = BytesIO()
        video_output = av.open(video_file, mode="w", format="mp4")
        ostream = video_output.add_stream_from_template(
            template=self.in_stream)
        logging.info("Codec name: " + self.in_stream.codec.name)
        video_snapshot = None
        if self.buffer:
            pts_ref = 0
            first_packet = True
            for packet in list(self.buffer.queue):
                if first_packet:
                    if packet.is_keyframe:
                        first_packet = False
                    else:
                        continue
                pts_ref += packet.duration
                packet.pts = pts_ref
                packet.dts = packet.pts
                packet.stream = ostream
                try:
                    video_output.mux(packet)
                except Exception as e:
                    logging.warning(f"Error muxing packet: {e}")
            video_output.close()
            video_file.seek(0)
            # with open("output.mp4", "wb") as f:
            #    f.write(video_file.getbuffer())
            #    return None
            video_snapshot = video_file
        self.video_in_progress = False
        return video_snapshot


def signal_handler(signal, frame):
    global RUNLOOP
    RUNLOOP = False


signal.signal(signal.SIGINT, signal_handler)


class CameraInstance:
    def __init__(self, bot: BotHandler, camera_id: int, rtsp_url: str):
        """
        Initialize the CameraInstance.

        :param bot: Telegram Bot instance for sending messages.
        :param rtsp_stream: VideoStream instance for handling video streaming.
        :param camera_id: ID of the camera.
        """
        self.bot = bot
        self.camera_id = camera_id
        self.rtsp_stream = None
        if config_data["cameras"][camera_id]["nomedia"] is False:
            self.rtsp_stream = VideoStream(rtsp_url)
            bot.rtsp_streams.append(self.rtsp_stream)

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
                mess_tree = ""
                for cur_message in messages.NotificationMessage:
                    mess_tree = cur_message.Message._value_1.Data.SimpleItem[0].Value
                if mess_tree == "true":
                    await self.bot.send_message(
                        f"""Motion detected on camera {self.camera_id} at {
                            time.strftime('%Y-%m-%d %H:%M:%S')}"""
                    )
                    if self.rtsp_stream is not None:
                        await self.bot.send_video(await self.rtsp_stream.video_snapshot())
            except Exception as e:
                logging.info(f"Exception {e} occurred. Retrying..")
        await manager.shutdown()


async def main():
    """
    Main function to initialize and run the bot and camera instances.

    This function sets up logging, creates an instance of the BotHandler,
    and initializes tasks for running the bot and handling video streams
    for each camera defined in the configuration.

    :return: None
    """
    logging.basicConfig(level=logging.INFO)

    tasks = []
    bots = {}

    for bot in config_data["bots"]:
        if bot == "telegram":
            bot_instance = TelegramBot(
                config_data["bots"][bot]["token"],
                config_data["bots"][bot]["channel_id"],
            )
        elif bot == "slack":
            bot_instance = SlackBot(
                config_data["bots"][bot]["token"],
                config_data["bots"][bot]["channel_id"],
            )
        bots[bot] = bot_instance
        tasks.append(asyncio.create_task(bot_instance.run()))

    for camera_name, camera_config in config_data["cameras"].items():
        rtsp_url = f"""rtsp://{camera_config['username']}:{
            quote(camera_config['password'])}@{camera_config['camera_ip']}:554/stream1"""
        if camera_config["bot"] in bots:
            bot_instance = bots[camera_config["bot"]]
            cam_instance = CameraInstance(
                bot_instance, camera_name, rtsp_url)
            tasks.append(asyncio.create_task(cam_instance.run()))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
