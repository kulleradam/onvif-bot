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

import logging
from slack_sdk.web.async_client import AsyncWebClient
import yaml
from pydantic import BaseModel, Field

bot_cfg = {}
cam_cfg = {}


class SlackBot:
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
        await self.slack_bot.files_upload_v2(
            channel=self.slack_channel_id, file=file, title=title
        )

    async def run(self):
        bot_name = await self.slack_bot.auth_test()
        logging.info("Slack bot name: " + bot_name["user"])
        await self.slack_bot.chat_postMessage(
            channel=self.slack_channel_id, text="Starting python node"
        )

    async def stop(self):
        pass


class TelegramBot:
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
        await bot.send_video(
            chat_id=self.telegram_channel_id, video=video, write_timeout=100
        )

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
        await self.telegram_bot.updater.stop()
        self.telegram_bot.updater.is_idle = False


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
        self.buffer = Queue()
        self.latest_keyframe = None
        self.ostream = None
        self.in_stream = None
        self.rtsp_url = rtsp_url
        self.video_in_progress = False
        self.stop_event = threading.Event()
        self.video_thread = threading.Thread(
            target=self.stream_capture, daemon=True)
        self.video_thread.start()

    def stream_capture(self):
        while not self.stop_event.is_set():
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
                    while (
                        self.buffer.qsize() >= queue_size
                        and self.video_in_progress is False
                    ):
                        self.buffer.get()
                    if packet.dts is None:
                        continue
                    if packet.is_keyframe:
                        self.latest_keyframe = packet
                    self.buffer.put(packet)
                    if self.stop_event.is_set():
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

    def stop(self):
        self.stop_event.set()
        self.video_thread.join()


class CameraInstance:
    def __init__(self, bot, camera_id: int, rtsp_url: str):
        """
        Initialize the CameraInstance.

        :param bot: Telegram Bot instance for sending messages.
        :param rtsp_stream: VideoStream instance for handling video streaming.
        :param camera_id: ID of the camera.
        """
        self.bot = bot
        self.camera_id = camera_id
        self.rtsp_stream = None
        self.stop_event = asyncio.Event()
        if cam_cfg[camera_id].nomedia is False:
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
            cam_cfg[self.camera_id].camera_ip,
            cam_cfg[self.camera_id].camera_onvif_port,
            cam_cfg[self.camera_id].username,
            cam_cfg[self.camera_id].password,
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

        while not self.stop_event.is_set():
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
                        await self.bot.send_video(
                            await self.rtsp_stream.video_snapshot()
                        )
            except asyncio.CancelledError:
                logging.info(f"Stopping camera {self.camera_id}")
                self.stop_event.set()
                if self.rtsp_stream is not None:
                    logging.info("Stopping video stream")
                    self.rtsp_stream.stop()
                await manager.shutdown()
            except Exception as e:
                logging.info(f"Exception {e} occurred. Retrying..")


async def main():
    """
    Main function to initialize and run the bot and camera instances.

    This function sets up logging, creates an instance of the Bot,
    and initializes tasks for running the bot and handling video streams
    for each camera defined in the configuration.

    :return: None
    """
    logging.basicConfig(level=logging.INFO)

    bots = {}
    camera_instances = {}
    tasks = []

    for bot in bot_cfg:
        if bot == "telegram":
            bot_instance = TelegramBot(
                bot_cfg[bot].token,
                bot_cfg[bot].channel_id,
            )
        elif bot == "slack":
            bot_instance = SlackBot(
                bot_cfg[bot].token,
                bot_cfg[bot].channel_id,
            )
        bots[bot] = bot_instance
        tasks.append(asyncio.create_task(bot_instance.run()))

    for camera_name, camera_config in cam_cfg.items():
        rtsp_url = f"""rtsp://{camera_config.username}:{
            quote(camera_config.password)}@{camera_config.camera_ip}:554/stream1"""
        if camera_config.bot in bots:
            bot_instance = bots[camera_config.bot]
            cam_instance = CameraInstance(bot_instance, camera_name, rtsp_url)
            camera_instances[camera_name] = cam_instance
            tasks.append(asyncio.create_task(cam_instance.run()))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass


def shutdown_handler(loop):
    print("\nShutting down gracefully...")
    for task in asyncio.all_tasks(loop):
        task.cancel()  # Cancel asyncio tasks


class BotConfig(BaseModel):
    token: str
    channel_id: int


class CameraInstanceConfig(BaseModel):
    camera_ip: str
    camera_onvif_port: int = Field(default=2020, ge=0, le=65535)
    username: str
    password: str
    bot: str
    nomedia: bool


if __name__ == "__main__":
    # Load and validate configuration from user_data.yaml
    try:
        with open(path.join(path.dirname(__file__), "user_data.yaml")) as file:
            config_data = yaml.safe_load(file)
        logging.info("Validating configuration...")
        bot_cfg = {
            name: BotConfig(**group_data)
            for name, group_data in config_data["bots"].items()
        }
        if not bot_cfg:
            logging.error("No bot configurations found in 'user_data.yaml'")
            exit(1)

        for camera_name, user_data in config_data["cameras"].items():
            if user_data["bot"] not in bot_cfg:
                logging.error(
                    f"Camera '{camera_name}' has an invalid bot configuration"
                )
                exit(1)
        cam_cfg = {
            name: CameraInstanceConfig(**camera_data)
            for name, camera_data in config_data["cameras"].items()
        }
        logging.info(
            f"Configuration validated successfully for {len(cam_cfg)} cameras and {
                len(bot_cfg)} bots."
        )

    except FileNotFoundError:
        print(
            """Error: 'user_data.yaml' file not found. Please refer to the 
            README for configuration instructions."""
        )
        exit(1)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Signal handling for graceful shutdown
    signal.signal(signal.SIGINT, lambda sig, frame: shutdown_handler(loop))

    try:
        loop.run_until_complete(main())
    except asyncio.CancelledError:
        pass
    finally:
        # Cancel all running tasks and close the loop
        tasks = asyncio.all_tasks(loop)
        for task in tasks:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
        print("Shutdown complete.")
