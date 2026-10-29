import time
from io import BytesIO

import requests
from PIL import Image

BASE = "http://127.0.0.1:8080"
session = requests.Session()
session.trust_env = False  # Connect directly to the local emulator.


def api(endpoint, **params):
    response = session.get(
        f"{BASE}/{endpoint}",
        params=params,
        timeout=5,
    )
    response.raise_for_status()
    return response


def screenshot(filename):
    image = Image.open(BytesIO(api("screen").content))
    image.save(filename)
    print(f"Saved {filename}: {image.size}")
    return image


print("Connection:", api("ping").text.strip())

screenshot("before.png")

# Ensure the emulator is running so the held button takes effect.
api("run")
api("input", Right=1)
try:
    time.sleep(0.5)
finally:
    api("input", Right=0)  # Always release the button.

time.sleep(0.2)
screenshot("after.png").show()

print("Done. Check the emulator and compare before.png with after.png.")