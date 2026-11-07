    .text
    .globl fixture_entry
    .globl fixture_hot
    .globl fixture_after

# A small deterministic body with a RIP-relative indirect call through the IAT.
# fixture_entry is the exported anchor; fixture_hot is the sampled site.
    .p2align 4
fixture_entry:
    pushq   %rbp
    movq    %rsp, %rbp
    subq    $0x20, %rsp
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
    .section .drectve,"yn"
    .ascii " /EXPORT:fixture_entry"
