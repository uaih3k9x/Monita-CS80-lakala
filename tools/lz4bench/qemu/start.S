/* qemu-system-arm -M virt 裸机入口 (ARM 状态): 栈、开 VFP/NEON (硬浮点 ABI)、清 bss, 跳 main (Thumb); 半主机退出 */
    .syntax unified
    .arm
    .section .text.start, "ax"
    .global _start
_start:
    ldr   sp, =__stack_top
    mrc   p15, 0, r0, c1, c0, 2      @ CPACR: cp10/cp11 全权限
    orr   r0, r0, #(0xF << 20)
    mcr   p15, 0, r0, c1, c0, 2
    isb
    mov   r0, #0x40000000            @ FPEXC.EN
    vmsr  fpexc, r0
    ldr   r0, =__bss_start
    ldr   r1, =__bss_end
    mov   r2, #0
1:  cmp   r0, r1
    strlo r2, [r0], #4
    blo   1b
    ldr   r0, =main
    blx   r0
    b     sh_exit

/* void sh_write0(const char *s): 半主机 SYS_WRITE0 */
    .global sh_write0
    .type sh_write0, %function
sh_write0:
    mov   r1, r0
    mov   r0, #0x04
    svc   0x123456
    bx    lr

/* void sh_exit(int code): 半主机 SYS_EXIT (ADP_Stopped_ApplicationExit) */
    .global sh_exit
    .type sh_exit, %function
sh_exit:
    mov   r0, #0x18
    ldr   r1, =0x20026
    svc   0x123456
2:  b     2b
