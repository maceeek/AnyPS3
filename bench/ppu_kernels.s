# PPU micro-benchmark kernels (big-endian PowerPC64, Cell-compatible subset).
# Data block at r30 (0x200000), 64 KiB. Each kernel loops ITER times via CTR.
	.text
	.globl k_copy
k_copy:                       # word copy loop, 4 KiB, lwzu/stwu
	lis 30,0x20
	li 5,0
	mtctr 3
1:	addi 6,30,-4
	addi 7,30,0x1000-4
	li 8,1024
2:	lwzu 9,4(6)
	stwu 9,4(7)
	addi 8,8,-1
	cmpwi 8,0
	bne 2b
	bdnz 1b
	blr

	.globl k_calls
k_calls:                      # call-heavy: prologue/epilogue leaf calls
	mflr 0
	std 0,16(1)
	stdu 1,-128(1)
	mtctr 3
	li 4,0
1:	mr 3,4
	bl leaf
	add 4,4,3
	bdnz 1b
	addi 1,1,128
	ld 0,16(1)
	mtlr 0
	blr
leaf:
	stdu 1,-64(1)
	std 31,56(1)
	addi 31,3,7
	rlwinm 3,31,3,0,28
	xor 3,3,31
	ld 31,56(1)
	addi 1,1,64
	blr

	.globl k_branchy
k_branchy:                    # compare/branch chains, rlwinm, CR logic
	lis 30,0x20
	mtctr 3
	li 4,0
	li 5,12345
1:	rlwinm 6,5,0,28,31
	cmplwi 6,5
	blt 2f
	cmplwi 6,10
	bgt 3f
	addi 4,4,3
	b 4f
2:	addi 4,4,1
	b 4f
3:	subf 4,6,4
4:	mulli 5,5,1103
	addi 5,5,12345
	clrlwi 5,5,1
	cmpw 1,4,5
	crand 2,4,6
	bdnz 1b
	mr 3,4
	blr

	.globl k_float
k_float:                      # scalar single FP: dot products over 4 KiB
	lis 30,0x20
	mtctr 3
1:	mr 6,30
	li 8,256
	lfs 0,0(30)
	fsubs 1,0,0
2:	lfs 2,0(6)
	lfs 3,4(6)
	lfs 4,8(6)
	fmuls 5,2,3
	fmadds 1,4,5,1
	fadds 1,1,2
	stfs 1,12(6)
	addi 6,6,16
	addi 8,8,-1
	cmpwi 8,0
	bne 2b
	bdnz 1b
	blr

	.globl k_vmx
k_vmx:                        # VMX: vmaddfp over 4 KiB (word-swap vperm keeps lanes finite)
	lis 30,0x20
	mtctr 3
1:	mr 6,30
	li 8,256
	li 9,16
	li 10,0x7ff0
	lvx 1,10,30
	lvx 2,9,30
	addi 7,30,0x4000
2:	lvx 3,0,6
	vmaddfp 4,3,2,2
	vperm 5,4,3,1
	vaddfp 4,4,5
	stvx 4,0,7
	addi 7,7,16
	addi 6,6,16
	addi 8,8,-1
	cmpwi 8,0
	bne 2b
	bdnz 1b
	blr

	.globl k_struct
k_struct:                     # struct walk: ld/lwz/lbz, conditional stores
	lis 30,0x20
	mtctr 3
1:	mr 6,30
	li 8,512
2:	lwz 9,0(6)
	lhz 10,4(6)
	lbz 11,6(6)
	add 9,9,10
	cmpwi 11,0
	beq 3f
	stw 9,0(6)
3:	addi 6,6,8
	addi 8,8,-1
	cmpwi 8,0
	bne 2b
	bdnz 1b
	blr

	.globl k_spin
k_spin:                       # guest busy-wait shape: or 31,31,31 blocks
	mtctr 3
1:	or 31,31,31
	or 31,31,31
	or 31,31,31
	or 31,31,31
	or 31,31,31
	or 31,31,31
	or 31,31,31
	or 31,31,31
	bdnz 1b
	blr
