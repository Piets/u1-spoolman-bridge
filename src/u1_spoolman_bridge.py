#!/usr/bin/env python3
import asyncio
import logging
import os
import re
from typing import Any

from moonraker_client import AsyncMoonrakerClient
import httpx


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

MOONRAKER_URL = os.environ["MOONRAKER_URL"].rstrip("/")
MOONRAKER_API_KEY = os.getenv("MOONRAKER_API_KEY")

SPOOLMAN_URL = os.environ["SPOOLMAN_URL"].rstrip("/")
SPOOLMAN_API_KEY = os.getenv("SPOOLMAN_API_KEY")  # only used if your client version supports it

# Moonraker object containing your NFC data.
FILAMENT_OBJECT = os.getenv("FILAMENT_OBJECT", "filament_detect")

# Available placeholders: {tool}, {spool_id}, {nfc_id}
ASSIGN_GCODE_TEMPLATE = os.getenv(
    "ASSIGN_GCODE_TEMPLATE",
    "SET_TOOL_SPOOL TOOL={tool} ID={spool_id}",
)

# Avoid repeatedly sending the same assignment.
DEBOUNCE_SECONDS = float(os.getenv("DEBOUNCE_SECONDS", "2.0"))


logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("u1-spoolman-bridge")


def normalize_single_hex(value: Any) -> str | None:
    """
    Normalize one NFC tag value:
      41:2F:BF:F0 -> 412FBFF0
      41 2f bf f0 -> 412FBFF0
      0x412fbff0  -> 412FBFF0
    """
    if value is None:
        return None

    if isinstance(value, list):
        return card_uid_to_hex(value)

    text = str(value).strip()
    if not text:
        return None

    text = text.removeprefix("0x").removeprefix("0X")
    text = re.sub(r"[^0-9a-fA-F]", "", text)
    return text.upper() or None


def normalize_nfc_ids(value: Any) -> list[str]:
    """
    Normalize Spoolman extra.nfc_id into a list of NFC IDs.

    Supports:
      "412FBFF0"
      "412FBFF0,F0BF2F41"
      "412FBFF0, F0BF2F41"
      "41:2F:BF:F0, F0:BF:2F:41"
    """
    if value is None:
        return []

    if isinstance(value, list):
        nfc_id = normalize_single_hex(value)
        return [nfc_id] if nfc_id else []

    ids: list[str] = []

    for part in str(value).split(","):
        nfc_id = normalize_single_hex(part)
        if nfc_id:
            ids.append(nfc_id)

    return ids


def card_uid_to_hex(card_uid: list[int]) -> str | None:
    """Convert Moonraker CARD_UID byte array to Spoolman nfc_id hex."""
    if not card_uid:
        return None

    bytes_ = list(card_uid)

    try:
        return "".join(f"{int(b) & 0xFF:02X}" for b in bytes_)
    except (TypeError, ValueError):
        return None


def get_extra_value(spool: dict[str, Any], key: str) -> Any:
    """
    Spoolman extra fields may appear as either:
      {"extra": {"nfc_id": "412FBFF0"}}
    or sometimes as richer field objects:
      {"extra": {"nfc_id": {"value": "412FBFF0"}}}
    """
    extra = spool.get("extra") or {}
    value = extra.get(key)

    if isinstance(value, dict) and "value" in value:
        return value["value"]

    return value


async def get_all_spools() -> list[dict[str, Any]]:
    """
    Fetch all spools directly from Spoolman's HTTP API.

    This avoids depending on the third-party Python client package version.
    """
    headers = {}

    if SPOOLMAN_API_KEY:
        headers["Authorization"] = f"Bearer {SPOOLMAN_API_KEY}"

    async with httpx.AsyncClient(
        base_url=SPOOLMAN_URL,
        headers=headers,
        timeout=20.0,
    ) as client:
        response = await client.get("/api/v1/spool")
        response.raise_for_status()
        data = response.json()

    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Spoolman spool response: {data!r}")

    return data


async def build_nfc_index() -> dict[str, dict[str, Any]]:
    """
    Build nfc_id -> spool lookup.
    """
    spools = await get_all_spools()

    index: dict[str, dict[str, Any]] = {}
    for spool in spools:
        if not isinstance(spool, dict):
            continue

        raw_nfc_ids = get_extra_value(spool, "nfc_id")
        nfc_ids = normalize_nfc_ids(raw_nfc_ids)

        for nfc_id in nfc_ids:
            if nfc_id in index:
                log.warning(
                    "Duplicate nfc_id %s found in Spoolman: %s and %s. Keeping first.",
                    nfc_id,
                    index[nfc_id].get("id"),
                    spool.get("id")
                )
                continue

            index[nfc_id] = spool

    log.info("Indexed %d Spoolman NFC tags", len(index))
    return index


class U1SpoolmanBridge:
    def __init__(self) -> None:
        self.moonraker: AsyncMoonrakerClient | None = None
        self.nfc_index_lock = asyncio.Lock()
        self.nfc_index: dict[str, dict[str, Any]] = {}
        self.last_assignment: dict[int, tuple[int | None, float]] = {}

    def create_moonraker_client(self) -> AsyncMoonrakerClient:
        client_kwargs: dict[str, Any] = {}
        if MOONRAKER_API_KEY:
            client_kwargs["api_key"] = MOONRAKER_API_KEY
        return AsyncMoonrakerClient(MOONRAKER_URL, **client_kwargs)

    async def find_spool_by_nfc(self, nfc_id: str) -> dict[str, Any] | None:
        """
        Look up a spool by NFC ID. If not found, refresh the Spoolman index once
        because a new spool may have been added after startup.
        """
        spool = self.nfc_index.get(nfc_id)
        if spool:
            return spool

        async with self.nfc_index_lock:
            # Another task may have refreshed the index while we were waiting.
            spool = self.nfc_index.get(nfc_id)
            if spool:
                return spool

            log.info(
                "NFC %s not found in current Spoolman index; refreshing index and retrying",
                nfc_id,
            )

            self.nfc_index = await build_nfc_index()
            return self.nfc_index.get(nfc_id)

    async def start(self) -> None:
        self.nfc_index = await build_nfc_index()
        client = self.create_moonraker_client()
        self.moonraker = client

        try:
            async with client:
                # Disable the library's built-in reconnect path. The current
                # moonraker-client version can start a second listener task
                # during reconnect, which causes websockets recv concurrency
                # failures. We reconnect from main() with a fresh client instead.
                await client.connect_websocket(reconnect=False)
                await client.identify("filament-detect-spoolman", "1.0.0")
    
                client.on("notify_status_update", self.on_status_update)
    
                result = await client.printer_objects_query({FILAMENT_OBJECT: None})
                await self.handle_filament_detect(
                    result.get("status", {}).get(FILAMENT_OBJECT)
                )
    
                await client.subscribe_objects({FILAMENT_OBJECT: None})
                log.info("Subscribed to Moonraker object %r", FILAMENT_OBJECT)
    
                while True:
                    await asyncio.sleep(1)
                    if client.websocket_connected:
                        continue
    
                    reason = getattr(client._ws, "connection_lost_reason", None)
                    if reason is None:
                        raise ConnectionError("Moonraker WebSocket disconnected")
                    raise ConnectionError(
                        f"Moonraker WebSocket disconnected: {reason}"
                    ) from reason
        finally:
            self.moonraker = None

    async def on_status_update(self, params: Any) -> None:
        """
        Moonraker notify_status_update params are commonly:
        [{object_status}, eventtime]
        """
        try:
            data = params[0] if isinstance(params, list) and params else params
            status = data.get(FILAMENT_OBJECT) if isinstance(data, dict) else None
            if status is not None:
                await self.handle_filament_detect(status)
        except Exception:
            log.exception("Failed to handle Moonraker status update: %r", params)

    async def handle_filament_detect(self, filament_detect: dict[str, Any] | None) -> None:
        if not filament_detect:
            return

        info = filament_detect.get("info") or []
        state = filament_detect.get("state") or []

        if not isinstance(info, list):
            log.warning("Unexpected %s.info format: %r", FILAMENT_OBJECT, info)
            return

        for tool_index, tag_info in enumerate(info):
            if not isinstance(tag_info, dict):
                continue

            card_uid = tag_info.get("CARD_UID")
            nfc_id = card_uid_to_hex(card_uid) if isinstance(card_uid, list) else None

            if not nfc_id:
                log.debug("Tool %d has no CARD_UID; state=%r", tool_index, state)
                continue

            spool = await self.find_spool_by_nfc(nfc_id)
            if not spool:
                log.warning(
                    "Tool %d NFC %s was not found in Spoolman extra.nfc_id even after refreshing index",
                    tool_index,
                    nfc_id,
                )
                continue

            spool_id = spool.get("id")
            if spool_id is None:
                log.warning("Matched NFC %s but spool has no id: %r", nfc_id, spool)
                continue

            await self.assign_spool(tool_index, int(spool_id), nfc_id, spool)

    async def assign_spool(
        self,
        tool_index: int,
        spool_id: int,
        nfc_id: str,
        spool: dict[str, Any],
    ) -> None:
        now = asyncio.get_running_loop().time()
        previous = self.last_assignment.get(tool_index)

        if previous and previous[0] == spool_id and now - previous[1] < DEBOUNCE_SECONDS:
            return

        self.last_assignment[tool_index] = (spool_id, now)

        log.info(
            "Assigning %s from NFC %s to tool %d",
            spool.get("id"),
            nfc_id,
            tool_index,
        )

        client = self.moonraker
        if client is None:
            raise ConnectionError("Moonraker client is not connected")

        gcode = ASSIGN_GCODE_TEMPLATE.format(
            tool=tool_index,
            spool_id=spool_id,
            nfc_id=nfc_id,
        )
        await client.gcode_script(gcode)


async def main() -> None:
    bridge = U1SpoolmanBridge()

    while True:
        try:
            await bridge.start()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Bridge crashed; reconnecting in 5 seconds")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
