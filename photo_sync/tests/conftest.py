"""Shared fixtures. Nothing here touches the network or real credentials."""

from __future__ import annotations

import io
import random
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

# Tests import the modules the way sync.py does, from the photo_sync root.
PHOTO_SYNC_DIR = Path(__file__).resolve().parent.parent
if str(PHOTO_SYNC_DIR) not in sys.path:
    sys.path.insert(0, str(PHOTO_SYNC_DIR))


def make_photo(seed: int, size=(900, 700)) -> Image.Image:
    """A deterministic, visually busy image. Different seeds look different."""
    rng = random.Random(seed)
    img = Image.new("RGB", size, (rng.randint(0, 80), rng.randint(0, 80), rng.randint(60, 160)))
    d = ImageDraw.Draw(img)
    for _ in range(160):
        x, y = rng.randint(0, size[0] - 20), rng.randint(0, size[1] - 20)
        d.ellipse(
            [x, y, x + rng.randint(8, 70), y + rng.randint(8, 70)],
            fill=(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)),
        )
    return img


def encode(img: Image.Image, quality: int = 92, **kwargs) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, **kwargs)
    return buf.getvalue()


@pytest.fixture
def photo_a() -> Image.Image:
    return make_photo(11)


@pytest.fixture
def photo_b() -> Image.Image:
    return make_photo(97)


def activity(
    entry_id: int,
    action: str = "checkout",
    who: str = "Nick Brown",
    when: str = "2026-07-17 09:01:45",
) -> dict:
    """An activity-log entry shaped like Snipe-IT's API response."""
    return {
        "id": entry_id,
        "action_type": action,
        "admin": {"name": who},
        "created_at": {"datetime": when, "formatted": "July 17, 2026 9:01am"},
    }
