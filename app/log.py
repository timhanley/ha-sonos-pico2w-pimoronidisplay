# Minimal leveled logging over the USB console.
from app.settings import DEBUG


def info(msg):
    print(msg)


def error(msg):
    print("ERROR:", msg)


def debug(msg):
    if DEBUG:
        print("DEBUG:", msg)
