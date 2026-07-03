# U1 Spoolman Bridge

Bridge NFC-based filament detection on a SnapMaker U1 printer into [Spoolman](https://github.com/Donkie/Spoolman) through Moonraker.

This service watches a Moonraker object for detected NFC tag UIDs, matches those tags against a custom `nfc_id` field in Spoolman, and sends G-code back to Moonraker to assign the matching spool to the active tool.

## Why Use This Project

- Automatically map NFC-tagged spools to Spoolman spool IDs
- Support multi-tool workflows by assigning spools per tool
- Refresh the Spoolman index automatically when a new NFC tag appears
- Run as a lightweight Docker container
- Stay resilient to Moonraker disconnects with automatic reconnect logic

## How It Works

1. The bridge subscribes to a Moonraker printer object, `filament_detect` by default.
2. When Moonraker reports an NFC `CARD_UID`, the bridge normalizes the UID into uppercase hex.
3. The bridge loads all Spoolman spools and looks for a matching `extra.nfc_id` value.
4. If a match is found, the bridge sends a configurable G-code command such as `SET_TOOL_SPOOL TOOL={tool} ID={spool_id}`.

## Requirements

- A running Moonraker instance
- A running Spoolman instance
- A Moonraker object that exposes NFC tag data, `filament_detect` by default
- A Klipper and Moonraker setup that can react to per-tool spool assignments
- Extended U1 Firmware from [https://snapmakeru1-extended-firmware.pages.dev](https://snapmakeru1-extended-firmware.pages.dev)

The example configs in [`example/`](./example/) are based on ideas from [unlucio/U1-klipper-configs](https://github.com/unlucio/U1-klipper-configs).

## Spoolman Setup

Create a custom Spoolman extra field named `nfc_id` with type `string`.

Supported formats include:

- `412FBFF0`
- `41:2F:BF:F0`
- `41 2f bf f0`
- `0x412fbff0`
- Comma-separated values such as `412FBFF0,F0BF2F41`

This lets a single spool match one or more NFC tags.

## Installation

### Option 1: Use The Published GHCR Image

Create a `docker-compose.yml`:

```yaml
services:
  u1-spoolman-bridge:
    image: ghcr.io/piets/u1-spoolman-bridge:latest
    container_name: u1-spoolman-bridge
    restart: unless-stopped
    environment:
      MOONRAKER_URL: http://moonraker.local
      SPOOLMAN_URL: http://spoolman.local
      ASSIGN_GCODE_TEMPLATE: SET_TOOL_SPOOL TOOL={tool} ID={spool_id}
      # Optional:
      # MOONRAKER_API_KEY: your-moonraker-api-key
      # SPOOLMAN_API_KEY: your-spoolman-api-key
      # FILAMENT_OBJECT: filament_detect
      # DEBOUNCE_SECONDS: "2.0"
```

Start the service:

```bash
docker compose up -d
```


### Option 2: Build Locally

The repository includes a local build example in [`docker-compose.yml`](./docker-compose.yml):

```bash
cp .env.example .env
```

```bash
docker compose up -d --build
```

## Configuration

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `MOONRAKER_URL` | Yes | none | Base URL of your Moonraker instance |
| `MOONRAKER_API_KEY` | No | none | API key for Moonraker if your instance requires one |
| `SPOOLMAN_URL` | Yes | none | Base URL of your Spoolman instance |
| `SPOOLMAN_API_KEY` | No | none | API key for Spoolman if required |
| `FILAMENT_OBJECT` | No | `filament_detect` | Moonraker object that exposes NFC state |
| `ASSIGN_GCODE_TEMPLATE` | No | `SET_TOOL_SPOOL TOOL={tool} ID={spool_id}` | G-code template sent to Moonraker |
| `DEBOUNCE_SECONDS` | No | `2.0` | Prevents repeated assignment of the same spool to the same tool |
| `LOG_LEVEL` | No | `INFO` | Python log level |

Available placeholders in `ASSIGN_GCODE_TEMPLATE`:

- `{tool}`
- `{spool_id}`
- `{nfc_id}`

## Klipper And Moonraker Integration

The bridge expects Moonraker and Klipper to already know how to store and activate a spool per tool.

- Moonraker example: [`example/moonraker.cfg`](./example/moonraker.cfg)
- Klipper example: [`example/klipper.cfg`](./example/klipper.cfg)

If you use the provided Klipper example, also create the referenced `variables.cfg` file:

- `~/printer_data/config/extended/variables.cfg`

### Example G-code Macro

```ini
[gcode_macro SET_TOOL_SPOOL]
description: Assign a Spoolman spool ID to a tool and persist it
gcode:
  {% set tool = params.TOOL|int %}
  {% set spool_id = params.ID|int %}

  {% if tool < 0 %}
    {action_raise_error("TOOL must be >= 0")}
  {% endif %}

  SET_GCODE_VARIABLE MACRO=T{tool} VARIABLE=spool_id VALUE={spool_id}
  SAVE_VARIABLE VARIABLE=t{tool}__spool_id VALUE={spool_id}

  RESPOND TYPE=echo MSG="Assigned Spoolman spool {spool_id} to T{tool}"
```

## NFC Tag Writing

For writing tags, the free iOS app SpoolFlux is a convenient option because it supports the OpenSpool format used by the extended U1 firmware.

Recommended tag type:

- `NTAG215`

## Development

### Build The Container

```bash
docker build -f src/Dockerfile -t u1-spoolman-bridge:local src
```

### Run The Script Directly

```bash
cd src
pip install -r requirements.txt
MOONRAKER_URL=http://moonraker.local \
SPOOLMAN_URL=http://spoolman.local \
python -u u1_spoolman_bridge.py
```

## Troubleshooting

- If no spool is assigned, confirm the spool has a matching `extra.nfc_id` value in Spoolman.
- If assignments repeat too often, increase `DEBOUNCE_SECONDS`.
- If the bridge never sees updates, confirm the Moonraker object name matches `FILAMENT_OBJECT`.
- If a spool is added after the container starts, the bridge will refresh its Spoolman index the first time it sees an unknown NFC tag.
