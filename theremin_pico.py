import micropython
import rp2
from machine import Pin


SENSE_PITCH_PIN = 26
SENSE_VOLUME_PIN = 27
KICK_PIN = 20
STATE_MACHINE_FREQ = 125_000_000
READING_REPS = 4390


@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW, out_init=rp2.PIO.OUT_LOW)
def pio_octo_pack_loop():
    wrap_target()

    set(pindirs, 1)
    pull(block)
    set(x, 7)

    label("main_loop")

    set(pindirs, 1)
    out(pins, 1)
    set(pins, 1) [31]
    set(pindirs, 0) [31]
    out(pins, 1)
    out(y, 1)
    jmp(not_y, "skip_jit")
    nop()
    label("skip_jit")

    mov(y, invert(null))

    label("timer")
    jmp(pin, "still_h")
    jmp("done_m")
    label("still_h")
    jmp(y_dec, "timer")
    label("done_m")

    in_(y, 15)
    out(y, 1)
    jmp(y_dec, "no_push")
    push()
    label("no_push")

    jmp(x_dec, "main_loop")
    wrap()


sense_pitch_pin = Pin(SENSE_PITCH_PIN, Pin.OUT, Pin.PULL_DOWN)
sense_volume_pin = Pin(SENSE_VOLUME_PIN, Pin.OUT, Pin.PULL_DOWN)
kick_pin = Pin(KICK_PIN, Pin.OUT, Pin.PULL_DOWN)

sm_pitch = rp2.StateMachine(
    0,
    pio_octo_pack_loop,
    freq=STATE_MACHINE_FREQ,
    set_base=sense_pitch_pin,
    out_base=kick_pin,
    jmp_pin=sense_pitch_pin,
    out_shiftdir=rp2.PIO.SHIFT_RIGHT,
)
sm_pitch.active(1)

sm_volume = rp2.StateMachine(
    1,
    pio_octo_pack_loop,
    freq=STATE_MACHINE_FREQ,
    set_base=sense_volume_pin,
    out_base=kick_pin,
    jmp_pin=sense_volume_pin,
    out_shiftdir=rp2.PIO.SHIFT_RIGHT,
)
sm_volume.active(1)


@micropython.native
def get_many_bursts(sm, kick_mode, num_reps):
    start = 1 if kick_mode < 0 else 0
    kick = 1 if kick_mode > 0 else 0

    cmd = 0
    for i in range(4):
        push_flag = 0 if (i % 2 == 1) else 1
        jit = 1 if (i & 2) else 0
        unit = (push_flag << 3) | (jit << 2) | (kick << 1) | start
        cmd |= unit << (i * 4)

    cmd |= cmd << 16

    rsum1 = 0
    rsum2 = 0
    loop_reps = int(num_reps / 8)
    for _ in range(loop_reps):
        sm.put(cmd)

        res = sm.get()
        res_sum = ((32767 + (32767 << 15)) - res)
        res = sm.get()
        res_sum += (32767 + (32767 << 15)) - res
        res = sm.get()
        res_sum += (32767 + (32767 << 15)) - res
        res = sm.get()
        res_sum += (32767 + (32767 << 15)) - res

        rsum1 += res_sum & 32767
        rsum2 += (res_sum >> 15) & 32767

    return (rsum1 + rsum2) / loop_reps * 16


def read_channel(sm):
    return get_many_bursts(sm, 0, READING_REPS)


def stream_readings():
    print("# pico_theremin_stream gp26,gp27")
    while True:
        pitch = read_channel(sm_pitch)
        volume = read_channel(sm_volume)
        print("{:.2f},{:.2f}".format(pitch, volume))


stream_readings()
