from __future__ import annotations

import json
import io
import re
import socket
from pathlib import Path
from typing import Any

import qrcode
from qrcode.image.pure import PyPNGImage


def wifi_qr_text(ssid: str, password: str) -> str:
    def field(value: str) -> str:
        escaped = re.sub(r'([\\;,:\"])', r'\\\1', value)
        return f'"{escaped}"' if re.fullmatch(r"[0-9a-fA-F]+", value) else escaped

    return f"WIFI:T:WPA;S:{field(ssid)};P:{field(password)};;"


def wifi_qr_png(ssid: str, password: str) -> bytes:
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                         box_size=8, border=4)
    code.add_data(wifi_qr_text(ssid, password).encode("utf-8"), optimize=0)
    code.make(fit=True)
    output = io.BytesIO()
    code.make_image(image_factory=PyPNGImage).save(output)
    return output.getvalue()


def network_request(path: Path | None, action: str, **values: Any) -> dict:
    if path is None:
        return {"available": False, "error": "Wi-Fi setup is available on the Jetson."}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(20)
            client.connect(str(path))
            client.sendall(json.dumps({"action": action, **values}).encode() + b"\n")
            data = bytearray()
            while b"\n" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 65536:
                    raise OSError("Invalid network helper response")
            return json.loads(data)
    except (OSError, ValueError) as exc:
        return {"available": False, "error": "Jetson Wi-Fi service is unavailable."}
