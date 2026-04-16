# Micropython program for Raspberry Pi Pico to measure very lower
# capacitances (tens of femtofarad) between GPIO26 and ground or
# between GPIO26 and GPIO20 by measuring how many nanoseconds it
# takes for the internal pull-down to pull the GPIO pin to zero.
#
# For capacitance to ground, this can detect if a person comes 
# into proximity of an electrode, provide the electrode is the size
# of a soda can or larger.
#
# Matthias Wandel April 2026

import rp2
from machine import Pin
import time

# PIO state machine program to do all the precise timing.  The PIO staate
# machine will actually do a group of 8 readings and pack the restuls
# into 4 words to take the load off of micropython.
# this bit written by Google Gemini AI -- with a lot of iterations and guidance.
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW, out_init=rp2.PIO.OUT_LOW)
def pio_octo_pack_loop():
    wrap_target()

    # 1. SETUP (2 instructions)
    set(pindirs, 1)         # set our sense line as output to drive it high.
    pull(block)             # get new order from python
    set(x, 7)               # Loop 8 times (7 down to 0)

    label("main_loop")      # Loop over order word

    # CONFIG (3 bits: Start, Kick, Jitter)
    set(pindirs, 1)         # set our sense line as output to drive it high.
    out(pins, 1)            # Bit 0: Start State
    set(pins, 1) [31]       # Charge
    set(pindirs, 0) [31]    # Discharge Start and delay before kick
    out(pins, 1)            # Bit 1: Kick
    out(y, 1)               # Bit 2: Jitter Flag
    jmp(not_y, "skip_jit")  # Because we can only count every other clock,
    nop()                   # We have the option of adding 1 clock of "jitter" to get
    label("skip_jit")       # more precision in the average in absence of noise.

    # Timeout value (max 32 bit -- too long -- gets stuck for a long time if
    # the GPIO pin is held high.
    mov(y, invert(null))

    label("timer")
    jmp(pin, "still_h")
    jmp("done_m")
    label("still_h")
    jmp(y_dec, "timer")     # Decrement loop checking the GPIO line.
    label("done_m")

    # PACK & CONDITIONAL PUSH
    in_(y, 15)              # Shift 15-bit count into ISR
    out(y, 1)               # Bit 3: THE PUSH FLAG
    jmp(y_dec, "no_push")
    push()                  # Push flag is only set every other iteration
    label("no_push")        # which combines two 15 bit words into 30 bits.
                            # Why not 32 bits?  Cause that triggers micropython
                            # to use "bigints" which are slow!

    jmp(x_dec, "main_loop")
    wrap()

# Setup the GPIO pins
pin26 = Pin(26, Pin.OUT, Pin.PULL_DOWN)
pin20 = Pin(20, Pin.OUT, Pin.PULL_DOWN)

sm0 = rp2.StateMachine(0, pio_octo_pack_loop, freq=125_000_000,
                      set_base=pin26,
                      out_base=pin20,
                      jmp_pin=pin26,
                      out_shiftdir=rp2.PIO.SHIFT_RIGHT) # Essential!

sm0.active(1)

# Create a second state machine to go in the opposite direction
# I should really take half the readings with the tx and rx pins reversed
sm1 = rp2.StateMachine(1, pio_octo_pack_loop, freq=125_000_000,
                      set_base=pin20,
                      out_base=pin26,
                      jmp_pin=pin20,
                      out_shiftdir=rp2.PIO.SHIFT_RIGHT) # Essential!

sm1.active(1)


@micropython.native
# Using @micropython.native gets aquisition speed to around 250 kilosamples
# per second.  Using @micropython.viper would be even faster and probably
# make grouping the readings in the PIO unnecessary, but I didn't know about
# @micropython.viper when I implemented this.
def get_many_bursts(sm, kick_mode, num_reps):
    # Setup the base bits
    start = 1 if kick_mode < 0 else 0
    kick  = 1 if kick_mode > 0 else 0

    # Pack the 32-bit command word
    # Each unit is: [PushFlag][Jit][Kick][Start]
    cmd = 0
    for i in range(4):
        push_flag = 0 if (i % 2 == 1) else 1
        jit = 1 if (i & 2) else 0
        unit = (push_flag << 3) | (jit << 2) | (kick << 1) | start
        # Shift in the next 4-bit unit
        cmd |= (unit << (i * 4))

    cmd |= cmd << 16 # Duplicate first half to second half

    rsum1 = rsum2 = 0
    loop_reps = int(num_reps/8)
    for reps in range (0,loop_reps):
        sm.put(cmd) # Ask for 8 readings.

        res = sm.get() # Get the 4 packed result words and add them up.
        res_sum = ((32767+(32767<<15)) - res )
        res = sm.get()
        res_sum = res_sum + ((32767+(32767<<15)) - res )
        res = sm.get()
        res_sum = res_sum + ((32767+(32767<<15)) - res )
        res = sm.get()
        res_sum = res_sum + ((32767+(32767<<15)) - res )

        rsum1 += res_sum & 32767
        rsum2 += (res_sum>>15) & 32767

    sumall = rsum1+rsum2

    #print(f"sum all:{sumall}")
    return sumall/loop_reps*16


def graph_capacitance():
    single_ended = True # Single ended vs cross capacitence mode.

    max_hashes = 150     # width of ascii graph
    hash_per_ns = 5      # Graph scale (can also be less than 1)
    grid_repeat_ns = 2   # nanoseconds between major grid lines.
    
    scale_width_ns = max_hashes / hash_per_ns

    fill = ("."+" "*(hash_per_ns-1))*grid_repeat_ns
    fill = fill[1:]

    HashSrc = ""
    GridSrc = ""
    while len(HashSrc) <= max_hashes+hash_per_ns*grid_repeat_ns*2:
        HashSrc = HashSrc + "$"+(hash_per_ns*grid_repeat_ns-1)*"#"
        GridSrc = GridSrc + ":"+fill

    baseline_ns = 200 # Left edge of graph (this gets dynamically adjusted)

    nums = ""
    count = 0

    while True:
        count += 1
        if single_ended:
            num_get = 4390*3 # roughly three 1/60ths of a second periods
            #start = time.ticks_ms()
            average_ns = get_many_bursts(sm0,0,num_get)
            #print("Elapsed:",time.ticks_ms()-start)
            t = time.ticks_ms() % 1000
            nums = f"{t:4} "
        else:
            # sm0 and sm1 are configured to have tx and tx lines switch
            # so by alternating the two, we can check both directions.
            # But switching direction offsts it just a little bit, so I should
            # for each reading do some one way and some the other. For the time
            # being, I just don't use the bidirectional mode.

            #if count & 1:
            #    sm_use = sm1
            #else:
            #    sm_use = sm0
            sm_use = sm0

            num_get = 4390*2

            # Kick low readings straddle kick high readings to make difference
            # less a function of changing trends.
            #start = time.ticks_ms()
            ns_kicklow = get_many_bursts(sm_use,-1,num_get/2)
            ns_kickhigh = get_many_bursts(sm_use,1,num_get)
            ns_kicklow = (ns_kicklow + get_many_bursts(sm_use,-1,num_get/2))*0.5
            average_ns = ns_kickhigh-ns_kicklow
            #print("Elapsed:",time.ticks_ms()-start)
            #ns_nokick = get_many_bursts(0,num_get/2)
            #print(f"{ns_nokick:6.2f}")
            #nums = f"{ns_kickhigh:6.2f}-{ns_kicklow:6.2f}="

        # Adjust the scale, as it can move quite a LOT but we want to be able
        # to see small changes.
        scale_adj = average_ns-baseline_ns
        if scale_adj < 0:
            if scale_adj > (-scale_width_ns*.4):
                baseline_ns -= 1/hash_per_ns # scroll for small offset
            else:
                print("---------------scale jump---------------------")
                baseline_ns += scale_adj # jump for large offset

        scale_adj = average_ns - (baseline_ns+max_hashes/hash_per_ns)
        if scale_adj > 0:
            if scale_adj < (scale_width_ns*.4):
                baseline_ns += 1/hash_per_ns
            else:
                print("---------------scale jump---------------------")
                baseline_ns += scale_adj

        # Figure out how many #'s in a bargraph line.
        numhashes = int((average_ns-baseline_ns)*hash_per_ns*2)
        if numhashes < 0: numhashes = 0
        if numhashes > max_hashes*2: numhashes = max_hashes*2
        odd = numhashes & 1 # We later append a '!' for odd values to represent half a '#'
        numhashes >>= 1

        GridOffset = int((baseline_ns%grid_repeat_ns)*hash_per_ns)

        HashStr = HashSrc[GridOffset:GridOffset+numhashes]+("!" if odd else"")
        GridStr = GridSrc[GridOffset+len(HashStr):GridOffset+max_hashes]

        print(nums+f"{average_ns:6.2f}", HashSrc[GridOffset:GridOffset+numhashes]+("!" if odd else"")+GridStr)

graph_capacitance()
