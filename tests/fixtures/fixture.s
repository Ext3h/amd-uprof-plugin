    .text
    .globl fixture_entry
    .globl fixture_hot
    .globl fixture_after
    .globl fixture_second
    .globl fixture_internal

# A small deterministic body with a RIP-relative indirect call through the IAT.
# fixture_entry is the exported anchor; fixture_hot is the sampled site.
# fixture_second is a second export; fixture_internal is only a .pdata runtime
# function start, so it exercises the pdata anchor kind.
    .p2align 4
fixture_entry:
    .seh_proc fixture_entry
    pushq   %rbp
    .seh_pushreg %rbp
    movq    %rsp, %rbp
    .seh_setframe %rbp, 0
    subq    $0x20, %rsp
    .seh_stackalloc 0x20
    .seh_endprologue
    movl    $1, %eax
    addl    $2, %eax
    xorl    %ecx, %ecx
    movl    %eax, -4(%rbp)
    addl    $3, %eax
    movl    %eax, -8(%rbp)
fixture_hot:
    callq   *__imp_ExitProcess(%rip)
fixture_after:
    addl    $4, %eax
    leave
    retq
    .seh_endproc

    .p2align 4
fixture_second:
    .seh_proc fixture_second
    pushq   %rbp
    .seh_pushreg %rbp
    movq    %rsp, %rbp
    .seh_setframe %rbp, 0
    subq    $0x10, %rsp
    .seh_stackalloc 0x10
    .seh_endprologue
    movl    $7, %eax
    addl    $5, %eax
    leave
    retq
    .seh_endproc

    .p2align 4
fixture_internal:
    .seh_proc fixture_internal
    pushq   %rbp
    .seh_pushreg %rbp
    movq    %rsp, %rbp
    .seh_setframe %rbp, 0
    subq    $0x10, %rsp
    .seh_stackalloc 0x10
    .seh_endprologue
    movl    $9, %eax
    subl    $2, %eax
    leave
    retq
    .seh_endproc

    .section .drectve,"yn"
    .ascii " /EXPORT:fixture_entry /EXPORT:fixture_second"
