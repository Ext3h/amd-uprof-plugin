/* defect_lab.exe --class <class> [--seconds S] [--load N] [--control]

Runs one defect class from lab_core.dll for S seconds (default 10). --load N first starts N
lab_busy.exe processes as background load. --control also runs the class's control variant and
prints nanoseconds per iteration for both, to check that the class reproduces its defect.
Classes: memory, lock, branch, os, vendor, wait, chain, efficient.
*/
#include "lab.h"

typedef u64 (*lab_fn)(u64 ms, int control);

LAB_IMPORT u64 lab_memory(u64 ms, int control);
LAB_IMPORT u64 lab_lock(u64 ms, int control);
LAB_IMPORT u64 lab_branch(u64 ms, int control);
LAB_IMPORT u64 lab_os(u64 ms, int control);
LAB_IMPORT u64 lab_vendor(u64 ms, int control);
LAB_IMPORT u64 lab_wait(u64 ms, int control);
LAB_IMPORT u64 lab_chain(u64 ms, int control);
LAB_IMPORT u64 lab_efficient(u64 ms, int control);

/* STARTUPINFOA is 104 bytes with cb first; PROCESS_INFORMATION is 24 bytes. */
__declspec(dllimport) int WINAPI CreateProcessA(const char *application, char *command,
                                                void *process_attributes,
                                                void *thread_attributes, int inherit,
                                                u32 flags, void *environment,
                                                const char *directory, void *startup,
                                                void *information);

static const struct { const char *name; lab_fn fn; } CLASSES[] = {
    {"memory", lab_memory}, {"lock", lab_lock}, {"branch", lab_branch}, {"os", lab_os},
    {"vendor", lab_vendor}, {"wait", lab_wait}, {"chain", lab_chain},
    {"efficient", lab_efficient},
};

static void out(const char *text)
{
    u32 length = 0, written;
    while (text[length])
        ++length;
    WriteFile(GetStdHandle((u32)-11), text, length, &written, 0);
}

static void out_number(u64 value)
{
    char digits[24];
    int at = 23;
    digits[at] = 0;
    do {
        digits[--at] = (char)('0' + value % 10u);
        value /= 10u;
    } while (value);
    out(digits + at);
}

static int same(const char *a, const char *b, u32 length)
{
    for (u32 i = 0; i < length; ++i)
        if (a[i] != b[i])
            return 0;
    return b[length] == 0;
}

static u64 parse_number(const char *text, u32 length)
{
    u64 value = 0;
    for (u32 i = 0; i < length && text[i] >= '0' && text[i] <= '9'; ++i)
        value = value * 10u + (u64)(text[i] - '0');
    return value;
}

static u64 ns_per_iteration(lab_fn fn, u64 ms, int control)
{
    long long start, stop, frequency;
    QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&start);
    u64 iterations = fn(ms, control);
    QueryPerformanceCounter(&stop);
    u64 ns = (u64)(stop - start) * 1000000000ull / (u64)frequency;
    return iterations ? ns / iterations : 0;
}

static void start_load(u64 count)
{
    for (u64 i = 0; i < count; ++i) {
        u64 startup[13] = {104}, information[3] = {0};
        char command[] = "lab_busy.exe";
        if (!CreateProcessA(0, command, 0, 0, 0, 0, 0, 0, startup, information))
            out("warning: could not start lab_busy.exe\n");
    }
}

void defect_lab_entry(void)
{
    const char *line = GetCommandLineA();
    const char *words[16];
    u32 lengths[16], count = 0;
    int quoted = 0;
    for (const char *p = line; *p && count < 16;) {
        while (*p == ' ')
            ++p;
        if (!*p)
            break;
        words[count] = p;
        while (*p && (quoted || *p != ' ')) {
            if (*p == '"')
                quoted = !quoted;
            ++p;
        }
        lengths[count] = (u32)(p - words[count]);
        ++count;
    }
    lab_fn fn = 0;
    u64 seconds = 10, load = 0;
    int control = 0;
    for (u32 i = 1; i < count; ++i) {
        if (same(words[i], "--control", lengths[i])) {
            control = 1;
        } else if (i + 1 < count && same(words[i], "--seconds", lengths[i])) {
            seconds = parse_number(words[i + 1], lengths[i + 1]);
            ++i;
        } else if (i + 1 < count && same(words[i], "--load", lengths[i])) {
            load = parse_number(words[i + 1], lengths[i + 1]);
            ++i;
        } else if (i + 1 < count && same(words[i], "--class", lengths[i])) {
            for (u32 c = 0; c < sizeof(CLASSES) / sizeof(CLASSES[0]); ++c)
                if (same(words[i + 1], CLASSES[c].name, lengths[i + 1]))
                    fn = CLASSES[c].fn;
            ++i;
        }
    }
    if (!fn) {
        out("usage: defect_lab --class memory|lock|branch|os|vendor|wait|chain|efficient "
            "[--seconds S] [--load N] [--control]\n");
        ExitProcess(2);
    }
    start_load(load);
    out("class ns/iteration: ");
    out_number(ns_per_iteration(fn, seconds * 1000u, 0));
    out("\n");
    if (control) {
        out("control ns/iteration: ");
        out_number(ns_per_iteration(fn, seconds * 1000u, 1));
        out("\n");
    }
    ExitProcess(0);
}
