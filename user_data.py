config_data = {
    "cameras": {  # You can add multiple cameras
        "Hikvision_IPCB140h_telegram": {  # Camera name
            "username": "<camera_username>",
            "password": "<camera_password>",
            "camera_ip": "<camera_ip_address>",
            "camera_onvif_port": 80,
            "bot": "telegram",  # Bot name must match with one of the bots in "bots" below
        },
        # "TPLink_C320WS_slack": {  # Camera name
        #    "username": "<camera_username>",
        #    "password": "<camera_password>",
        #    "camera_ip": "<camera_ip_address>",
        #    "camera_onvif_port": 2020,
        #    "bot": "slack",  # Bot name must match with one of the bots in "bots" below
        # },
    },
    "bots": {
        "telegram": {
            "token": "<telegram_bot_token>",
            "channel_id": "<telegram_channel_id>",
        },
        # "slack": {
        #    "token": "xoxb-********",  # Bot User OAuth Token
        #    "channel_id": "<channel_id>",
        # },
    }
}
