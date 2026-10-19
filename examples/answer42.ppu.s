# Original synthetic test function. GNU/LLVM PowerPC assembly syntax.
# The ELF wrapper and PS3 OPD descriptor are produced by `anyps3 demo`.
.text
.globl answer42
answer42:
    li 3, 40
    li 4, 2
    add 3, 3, 4
    blr
