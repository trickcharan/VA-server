import os

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config')


class AudioUtils:

    @staticmethod
    def get_default_audio():
        audio_dir = os.path.join(CONFIG_DIR, "audio")
        with open(os.path.join(audio_dir, "recorded_voice.wav"), "rb") as audio_file:
            return audio_file.read()
