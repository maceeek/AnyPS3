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

	.globl k_atomic
k_atomic:                     # lwarx/stwcx. increment (uncontended atomic add, mutex shape)
	lis 30,0x20
	mtctr 3
1:	lwarx 5,0,30
	addi 5,5,1
	stwcx. 5,0,30
	bne- 1b
	bdnz 1b
	blr

# ---- Kernels shaped like compiled game code (appended; the kernels above keep their bytes).
# Symbols e_* are indirect-call/switch targets: translated as extra entries, not run as kernels.
# x_* live in the next 4 KiB page, t_* in the page after it (cross-page bl/blr pairs).

	.globl k_xcall
k_xcall:                      # cross-page bl/blr: leaf callee with a stack frame in another page
	mflr 0
	std 0,16(1)
	stdu 1,-128(1)
	mtctr 3
	li 4,0
1:	mr 3,4
	bl x_leaf
	add 4,4,3
	bdnz 1b
	mr 3,4
	addi 1,1,128
	ld 0,16(1)
	mtlr 0
	blr

	.globl k_calltree
k_calltree:                   # non-leaf cross-page callee calling a leaf in its page and one in a third page
	mflr 0
	std 0,16(1)
	std 31,-8(1)
	stdu 1,-128(1)
	mtctr 3
	li 31,0
1:	mr 3,31
	bl x_node
	add 31,31,3
	bdnz 1b
	mr 3,31
	addi 1,1,128
	ld 0,16(1)
	ld 31,-8(1)
	mtlr 0
	blr

	.globl k_virtual
k_virtual:                    # C++ virtual calls: this->vtable->slot1 -> descriptor {code,toc}, bctrl
	mflr 0
	std 0,16(1)
	std 31,-8(1)
	std 30,-16(1)
	std 29,-24(1)
	stdu 1,-144(1)
	mr 29,3
	bcl 20,31,1f
1:	mflr 9
	lis 30,0x30                 # objects 0x300000, vtables 0x300100, descriptors 0x300200
	addi 10,9,e_v0-1b
	stw 10,0x200(30)
	addi 10,9,e_v1-1b
	stw 10,0x208(30)
	addi 10,9,e_v2-1b
	stw 10,0x210(30)
	addi 10,9,e_v3-1b
	stw 10,0x218(30)
	li 10,0
	stw 10,0x204(30)
	stw 10,0x20c(30)
	stw 10,0x214(30)
	stw 10,0x21c(30)
	addi 10,30,0x200
	stw 10,0x104(30)
	addi 10,30,0x208
	stw 10,0x114(30)
	addi 10,30,0x210
	stw 10,0x124(30)
	addi 10,30,0x218
	stw 10,0x134(30)
	addi 10,30,0x100
	stw 10,0(30)
	addi 10,30,0x110
	stw 10,16(30)
	addi 10,30,0x120
	stw 10,32(30)
	addi 10,30,0x130
	stw 10,48(30)
	li 10,3
	stw 10,4(30)
	li 10,5
	stw 10,20(30)
	li 10,7
	stw 10,36(30)
	li 10,11
	stw 10,52(30)
	li 31,0
2:	rlwinm 11,29,4,26,27
	add 3,30,11
	lwz 9,0(3)
	lwz 11,4(9)
	lwz 0,0(11)
	std 2,40(1)
	lwz 2,4(11)
	mtctr 0
	bctrl
	ld 2,40(1)
	add 31,31,3
	addi 29,29,-1
	cmpwi 29,0
	bne 2b
	mr 3,31
	addi 1,1,144
	ld 0,16(1)
	ld 29,-24(1)
	ld 30,-16(1)
	ld 31,-8(1)
	mtlr 0
	blr
	.globl e_v0
e_v0:                         # getter in the caller's page
	lwz 4,4(3)
	addi 3,4,1
	blr

	.globl k_switch
k_switch:                     # switch through a jump table of relative offsets after the bctr
	mflr 0
	mr 29,3
	li 4,0
	li 5,12345
	bcl 20,31,1f
1:	mflr 8
	addi 8,8,3f-1b
2:	rlwinm 9,5,2,27,29
	lwzx 10,9,8
	add 10,10,8
	mtctr 10
	bctr
3:	.long e_s0-3b, e_s1-3b, e_s2-3b, e_s3-3b, e_s4-3b, e_s5-3b, e_s6-3b, e_s7-3b
	.globl e_s0, e_s1, e_s2, e_s3, e_s4, e_s5, e_s6, e_s7
e_s0:	addi 4,4,1
	b 4f
e_s1:	xor 4,4,5
	b 4f
e_s2:	subf 4,5,4
	b 4f
e_s3:	rlwinm 4,4,1,0,31
	b 4f
e_s4:	addi 4,4,-3
	b 4f
e_s5:	or 4,4,5
	b 4f
e_s6:	andi. 6,5,0xff
	add 4,4,6
	b 4f
e_s7:	srwi 6,5,3
	add 4,4,6
4:	mulli 5,5,1103
	addi 5,5,12345
	clrlwi 5,5,1
	addi 29,29,-1
	cmpwi 29,0
	bne 2b
	mr 3,4
	mtlr 0
	blr

	.globl k_mix
k_mix:                        # record loop: loads/stores with rlwinm field extraction, compares, branches
	lis 30,0x20
	mtctr 3
1:	mr 6,30
	addi 7,30,0x4000
	li 8,512
	li 12,0
2:	lwz 9,0(6)
	lwz 10,4(6)
	rlwinm 11,9,8,24,31
	cmplwi 11,0x3f
	bne 3f
	rlwinm 11,10,16,24,31
	cmplwi 1,11,0x80
	blt 1,4f
	lbz 11,5(6)
	add 12,12,11
	sth 12,0(7)
	b 5f
4:	rlwimi 12,10,4,20,27
	stw 12,0(7)
	b 5f
3:	addi 12,12,1
5:	clrlwi 12,12,16
	addi 6,6,8
	addi 7,7,4
	addi 8,8,-1
	cmpwi 8,0
	bne 2b
	bdnz 1b
	mr 3,12
	blr

	.p2align 12
x_leaf:                       # next page: leaf with a frame (callee of k_xcall and x_node)
	stdu 1,-64(1)
	std 31,56(1)
	addi 31,3,7
	rlwinm 3,31,3,0,28
	xor 3,3,31
	ld 31,56(1)
	addi 1,1,64
	blr
x_node:                       # non-leaf: calls x_leaf (same page) and t_leaf (third page)
	mflr 0
	std 0,16(1)
	std 30,-16(1)
	std 31,-8(1)
	stdu 1,-112(1)
	mr 31,3
	bl x_leaf
	mr 30,3
	addi 3,31,5
	bl t_leaf
	add 3,3,30
	addi 1,1,112
	ld 0,16(1)
	ld 30,-16(1)
	ld 31,-8(1)
	mtlr 0
	blr
	.globl e_v1, e_v2
e_v1:	lwz 4,4(3)
	slwi 3,4,1
	blr
e_v2:	lwz 4,4(3)
	addi 4,4,9
	xor 3,4,3
	blr

	.p2align 12
t_leaf:                       # third page
	addi 3,3,3
	rlwinm 3,3,0,1,31
	blr
	.globl e_v3
e_v3:	stdu 1,-48(1)
	lwz 4,4(3)
	mulli 3,4,3
	addi 1,1,48
	blr
