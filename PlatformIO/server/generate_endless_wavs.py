import os
import time

# Repeatedly call the following command every 5 seconds. 
# curl http://localhost:30000/api/buffer_to_local_wav

wav_directory = "serverwavs"

MAX_WAVS = 5

if __name__ == "__main__":
    while True:
        os.system("curl http://localhost:30000/api/buffer_to_local_wav")
        if len(os.listdir(wav_directory)) > MAX_WAVS:
            print(f"Too many WAV files in {wav_directory}, deleting oldest.")
            files = os.listdir(wav_directory)
            files.sort(key=lambda x: os.path.getctime(os.path.join(wav_directory, x)))
            os.remove(os.path.join(wav_directory, files[0]))
        time.sleep(5)
    