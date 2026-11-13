/* lab_busy.exe: background load for the defect_lab captures. Spins for 60 seconds in three
   functions of different weight, so each copy of the image shows several hot functions.
   defect_lab --load N starts N copies; a recording may also start copies under other names. */
#include "lab.h"

static volatile u64 sink;

__attribute__((noinline)) static u64 busy_hash(u64 x, u32 rounds)
{
    for (u32 i = 0; i < rounds; ++i)
        x = x * 6364136223846793005ull + 1u;
    return x;
}

__attribute__((noinline)) static u64 busy_mix(u64 x, u32 rounds)
{
    for (u32 i = 0; i < rounds; ++i)
        x ^= (x << 7) ^ (x >> 9);
    return x;
}

__attribute__((noinline)) static u64 busy_sum(u64 x, u32 rounds)
{
    for (u32 i = 0; i < rounds; ++i)
        x += i * 3u;
    return x;
}

void lab_busy_entry(void)
{
    u64 x = 1, end = GetTickCount64() + 60000u;
    while (GetTickCount64() < end) {
        x = busy_hash(x, 60000u);
        x = busy_mix(x, 30000u);
        x = busy_sum(x, 10000u);
    }
    sink = x;
    ExitProcess(0);
}
