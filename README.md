# ONVIF-Bot

[![Codacy Badge](https://api.codacy.com/project/badge/Grade/7e52f88546764858807cd56467cd9cc1)](https://app.codacy.com/gh/kulleradam/onvif-bot?utm_source=github.com&utm_medium=referral&utm_content=kulleradam/onvif-bot&utm_campaign=Badge_Grade)

ONVIF-Bot is a tool that integrates IP cameras with Telegram and Signal bots, enabling real-time video transmission on RTSP motion event triggers. Perfect for security and automation projects, this bot simplifies the process of receiving alerts and videos directly from your IP cameras.  
**Early stage:** currently supports HiVision IPC-B140H. TP-Link camera support in progress.

## Features

- Motion Detection: Responds to ONVIF-compatible motion events from IP cameras.
- RTSP Stream Integration: Captures video feeds and prepares clips for transmission.
- Multi-platform Support: Sends notifications and videos through Telegram or Signal bots.

## Use Cases

- Home security monitoring.
- Real-time alerts for surveillance.
- Smart image and video notifications via messaging apps—no subscriptions required.

## Chat commands

`/grabimage`: Captures an image from the RTSP stream and sends it to the Telegram channel.  
`/grabvideo`: Captures a video from the RTSP stream and sends it to the Telegram channel.
