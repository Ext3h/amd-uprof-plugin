/* lab_core.dll: the user's module of defect_lab. One function per defect class.

Each lab_<class>(ms, control) runs its class's code for about ms milliseconds and returns the
number of iterations it completed. control != 0 runs the control variant: the same work shape
without the defect (cache-resident data, no contention, predictable branches, independent
accumulators), so the caller can compare nanoseconds per iteration.
*/
#include "lab.h"

#define CHECK_EVERY 4096u

/* The compiler references _fltused from any floating-point code; without the C runtime the
   module defines it. */
int _fltused = 0;

LAB_IMPORT u64 vendor_work(u64 rounds);

static volatile u64 sink;

static u64 xorshift(u64 *state)
{
    u64 x = *state;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    return *state = x;
}

/* memory: random loads over 256 MiB, larger than any current last-level cache. */
LAB_EXPORT u64 lab_memory(u64 ms, int control)
{
    static u64 *table;
    u64 words = control ? 512u : (256u << 20) / sizeof(u64);
    if (!table)
        table = (u64 *)VirtualAlloc(0, (256u << 20), 0x3000u, 0x04u);
    u64 state = 88172645463325252ull, sum = 0, iterations = 0;
    u64 end = GetTickCount64() + ms;
    do {
        for (u32 i = 0; i < CHECK_EVERY; ++i)
            sum += table[xorshift(&state) % words];
        iterations += CHECK_EVERY;
    } while (GetTickCount64() < end);
    sink = sum;
    return iterations;
}

/* lock: two threads take a spin lock with lock cmpxchg and spin with pause while it is held. */
static volatile long lock_word;
static volatile u64 lock_counter;
static volatile u64 lock_end;

static void spin_lock(void)
{
    while (__sync_val_compare_and_swap(&lock_word, 0, 1) != 0)
        while (lock_word)
            __builtin_ia32_pause();
}

static u32 WINAPI lock_thread(void *parameter)
{
    u64 iterations = 0;
    (void)parameter;
    do {
        for (u32 i = 0; i < CHECK_EVERY; ++i) {
            spin_lock();
            lock_counter += 1;
            lock_word = 0;
        }
        iterations += CHECK_EVERY;
    } while (GetTickCount64() < lock_end);
    return (u32)iterations;
}

LAB_EXPORT u64 lab_lock(u64 ms, int control)
{
    lock_end = GetTickCount64() + ms;
    HANDLE other = control ? 0 : CreateThread(0, 0, lock_thread, 0, 0, 0);
    u64 iterations = lock_thread(0);
    if (other) {
        WaitForSingleObject(other, INFINITE_WAIT);
        CloseHandle(other);
    }
    return iterations;
}

/* branch: a conditional jump on random data. The taken side calls a function, which keeps the
   compiler from turning the branch into a conditional move. */
__attribute__((noinline)) static u64 taken(u64 x) { return x * 3u + 1u; }

LAB_EXPORT u64 lab_branch(u64 ms, int control)
{
    u64 state = 2463534242ull, sum = 0, iterations = 0;
    u64 end = GetTickCount64() + ms;
    do {
        if (control) {
            /* Alternating, so the predictor learns it. */
            for (u32 i = 0; i < CHECK_EVERY; ++i) {
                u64 x = xorshift(&state);
                if (i & 1u)
                    sum += taken(x);
                else
                    sum ^= x;
            }
        } else {
            for (u32 i = 0; i < CHECK_EVERY; ++i) {
                u64 x = xorshift(&state);
                if (x & 1u)
                    sum += taken(x);
                else
                    sum ^= x;
            }
        }
        iterations += CHECK_EVERY;
    } while (GetTickCount64() < end);
    sink = sum;
    return iterations;
}

/* os: one heap allocation and free per iteration; the time goes to ntdll. */
LAB_EXPORT u64 lab_os(u64 ms, int control)
{
    HANDLE heap = GetProcessHeap();
    u64 iterations = 0;
    u64 end = GetTickCount64() + ms;
    do {
        for (u32 i = 0; i < CHECK_EVERY / 16u; ++i) {
            if (control) {
                sink += i;
                continue;
            }
            void *block = HeapAlloc(heap, 0, 64u + (i & 255u));
            HeapFree(heap, 0, block);
        }
        iterations += CHECK_EVERY / 16u;
    } while (GetTickCount64() < end);
    return iterations;
}

/* vendor: calls the hot export of lab_vendor.dll. */
LAB_EXPORT u64 lab_vendor(u64 ms, int control)
{
    u64 iterations = 0;
    u64 end = GetTickCount64() + ms;
    do {
        sink += control ? iterations : vendor_work(256u);
        iterations += 1;
    } while (GetTickCount64() < end);
    return iterations;
}

/* wait: blocks on an event that is never set, with a little work between the waits, so the
   module still has samples while most of the wall time is off-CPU. */
LAB_EXPORT u64 lab_wait(u64 ms, int control)
{
    HANDLE event = CreateEventA(0, 1, 0, 0);
    u64 state = 1, iterations = 0;
    u64 end = GetTickCount64() + ms;
    do {
        for (u32 i = 0; i < 20000u; ++i)
            sink += xorshift(&state);
        if (!control)
            WaitForSingleObject(event, 5u);
        iterations += 1;
    } while (GetTickCount64() < end);
    CloseHandle(event);
    return iterations;
}

/* chain: a serial multiply/divide chain whose divisor is only known at run time. The control
   splits it into four independent chains. */
static volatile u64 divisor = 7u;

LAB_EXPORT u64 lab_chain(u64 ms, int control)
{
    u64 a = 12345u, b = 23456u, c = 34567u, d = 45678u, iterations = 0;
    u64 m = divisor;
    u64 end = GetTickCount64() + ms;
    do {
        for (u32 i = 0; i < CHECK_EVERY; ++i) {
            if (control) {
                a = a * 2654435761u / m + 1u;
                b = b * 2654435761u / m + 1u;
                c = c * 2654435761u / m + 1u;
                d = d * 2654435761u / m + 1u;
                i += 3u;
            } else {
                a = a * 2654435761u / m + 1u;
            }
        }
        iterations += CHECK_EVERY;
    } while (GetTickCount64() < end);
    sink = a + b + c + d;
    return iterations;
}

/* efficient: a vectorizable multiply-add over arrays that fit in L1. */
static float va[1024], vb[1024], vc[1024];
static volatile float seed = 1.5f;

LAB_EXPORT u64 lab_efficient(u64 ms, int control)
{
    u64 iterations = 0;
    u64 end = GetTickCount64() + ms;
    (void)control;
    /* Filled at run time, so the compiler cannot fold the multiply away. */
    for (u32 i = 0; i < 1024u; ++i) {
        va[i] = seed * (float)(i & 7u);
        vb[i] = seed / (float)(1u + (i & 3u));
    }
    do {
        for (u32 r = 0; r < 64u; ++r)
            for (u32 i = 0; i < 1024u; ++i)
                vc[i] = va[i] * vb[i] + vc[i];
        iterations += 64u * 1024u;
    } while (GetTickCount64() < end);
    sink = (u64)vc[iterations & 1023u];
    return iterations;
}

int WINAPI lab_core_entry(void *instance, u32 reason, void *reserved)
{
    (void)instance;
    (void)reason;
    (void)reserved;
    return 1;
}
