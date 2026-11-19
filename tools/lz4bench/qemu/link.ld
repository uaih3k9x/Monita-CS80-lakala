/* qemu virt: RAM 从 0x40000000 起 (默认 128 MiB) */
ENTRY(_start)
SECTIONS {
    . = 0x40010000;
    .text : { *(.text.start) *(.text*) }
    .rodata : { *(.rodata*) }
    .data : { *(.data*) }
    . = ALIGN(256);
    __bss_start = .;
    .bss : { *(.bss*) *(COMMON) }
    . = ALIGN(4);
    __bss_end = .;
    . = ALIGN(8) + 0x10000;
    __stack_top = .;
}
