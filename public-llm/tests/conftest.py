from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_tv import FIXTURE_MAC, FakeTV  # noqa: E402
from quiettv_mcp.config import SCHEMA, validate_config  # noqa: E402
from quiettv_mcp.device import QuietTV  # noqa: E402

EXAMPLE_IP = "10.20.30.40"


def config_dict(key_file: Path, *, ip: str = "127.0.0.1", **extra) -> dict:
    data = {
        "schema": SCHEMA,
        "profile": "UN43U8000FBXZA-1300.8",
        "tv": {"ip": ip, "mac": FIXTURE_MAC, "model": "UN43U8000FBXZA", "firmware": "1300.8"},
        "control_key_file": str(key_file),
    }
    data.update(extra)
    return data


@pytest.fixture
def key() -> str:
    return "fixture-" + secrets.token_hex(20)


@pytest.fixture
def key_file(tmp_path: Path, key: str) -> Path:
    path = tmp_path / "control.key"
    path.write_text(key + "\n", encoding="ascii")
    return path


@pytest.fixture
def fake(key: str):
    tv = FakeTV(key)
    yield tv
    tv.close()


@pytest.fixture
def config(key_file: Path, tmp_path: Path):
    return validate_config(config_dict(key_file, capture_dir=str(tmp_path / "captures")), allow_loopback_fixture=True)


@pytest.fixture
def device(config, fake):
    return QuietTV(config, identity_port=fake.identity_port, control_port=fake.control_port, timeout=5)


@pytest.fixture
def config_file(tmp_path: Path, key_file: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_dict(key_file, ip=EXAMPLE_IP)), encoding="utf-8")
    return path
