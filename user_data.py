config_data = {
    "cameras": {
        "Hikvision_228": {
            "username": "<camera_username>",
            "password": "<camera_password>",
            "camera_ip": "<camera_ip_address>",
            "camera_onvif_port": <camera_onvif_port>,
            "bot": "telegram",
        },
        #"TPLink_214": {
        #    "username": "<camera_username>",
        #    "password": "<camera_password>",
        #    "camera_ip": "<camera_ip_address>",
        #    "camera_onvif_port": <camera_onvif_port>,
        #    "bot": "telegram",
        #},
    },
    "bots": {
        "telegram": {
            "token": "<telegram_bot_token>",
            "channel_id": "<telegram_channel_id>",
        }
    }
}
