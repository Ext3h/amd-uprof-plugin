/* Freestanding declarations shared by the defect_lab images: the few kernel32 calls they make. */
#ifndef LAB_H
#define LAB_H

typedef unsigned long long u64;
typedef unsigned int u32;
typedef void *HANDLE;

#define WINAPI __stdcall
#define LAB_EXPORT __declspec(dllexport)
#define LAB_IMPORT __declspec(dllimport)
#define INFINITE_WAIT 0xFFFFFFFFu

__declspec(dllimport) u64 WINAPI GetTickCount64(void);
__declspec(dllimport) int WINAPI QueryPerformanceCounter(long long *count);
__declspec(dllimport) int WINAPI QueryPerformanceFrequency(long long *frequency);
__declspec(dllimport) HANDLE WINAPI GetProcessHeap(void);
__declspec(dllimport) void *WINAPI HeapAlloc(HANDLE heap, u32 flags, u64 bytes);
__declspec(dllimport) int WINAPI HeapFree(HANDLE heap, u32 flags, void *memory);
__declspec(dllimport) void *WINAPI VirtualAlloc(void *address, u64 bytes, u32 type, u32 protect);
__declspec(dllimport) HANDLE WINAPI CreateThread(void *attributes, u64 stack,
                                                 u32(WINAPI *start)(void *), void *parameter,
                                                 u32 flags, u32 *thread_id);
__declspec(dllimport) HANDLE WINAPI CreateEventA(void *attributes, int manual, int initial,
                                                 const char *name);
__declspec(dllimport) u32 WINAPI WaitForSingleObject(HANDLE handle, u32 milliseconds);
__declspec(dllimport) int WINAPI CloseHandle(HANDLE handle);
__declspec(dllimport) HANDLE WINAPI GetStdHandle(u32 which);
__declspec(dllimport) int WINAPI WriteFile(HANDLE file, const void *data, u32 bytes,
                                           u32 *written, void *overlapped);
__declspec(dllimport) char *WINAPI GetCommandLineA(void);
__declspec(dllimport) void WINAPI ExitProcess(u32 code);

#endif
