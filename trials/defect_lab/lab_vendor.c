/* lab_vendor.dll: stands in for third-party code the user's module calls. One hot export. */
#include "lab.h"

LAB_EXPORT u64 vendor_work(u64 rounds)
{
    u64 x = rounds;
    for (u64 i = 0; i < rounds * 64u; ++i)
        x = x * 6364136223846793005ull + 1442695040888963407ull;
    return x;
}

int WINAPI lab_vendor_entry(void *instance, u32 reason, void *reserved)
{
    (void)instance;
    (void)reason;
    (void)reserved;
    return 1;
}
