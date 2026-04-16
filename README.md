# Pico Theremin

This uses two capacitive sense wires on the Pico and generates the audio on your Mac.

## Wiring

- Pitch wire: `GP26`
- Volume wire: `GP27`
- Ground: optional common ground reference near the player

The current Pico script measures each wire to ground in single-ended mode.

## First Run

Install the Pico scripts onto the Pico:

```bash
python3 theremin_mac.py --install-pico
```

That writes `theremin_pico.py` and sets the Pico `main.py` to auto-start the streamer on boot.

Then run the theremin on the Mac:

```bash
python3 theremin_mac.py
```

The Mac script now starts `theremin_pico.py` over serial each time, so it does not depend on the Pico auto-starting the streamer at boot.

## Calibration

To watch raw values and collect min/max ranges:

```bash
python3 theremin_mac.py --calibrate
```

Move each hand through the range you want, note the raw min/max values, then edit `theremin_config.json`.

- `pitch_hand.raw_min` / `pitch_hand.raw_max` control pitch range mapping
- `volume_hand.raw_min` / `volume_hand.raw_max` control volume range mapping
- `pitch_hand.hz_min` / `pitch_hand.hz_max` set the note range
- `volume_hand.level_max` sets the loudest output level
- `invert` flips a hand direction if needed

## Useful Commands

List audio devices:

```bash
python3 theremin_mac.py --list-audio-devices
```

Reinstall the Pico script and boot file:

```bash
python3 theremin_mac.py --install-pico
```

## Recovery

If the Pico shows up as the `RPI-RP2` storage drive, it is in BOOTSEL mode rather than MicroPython serial mode. Flash MicroPython by copying the UF2 onto that drive, then unplug and replug the Pico normally without holding BOOTSEL before running `theremin_mac.py`.
