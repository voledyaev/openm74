	CPU 80C167
;--- Stage-1: 32-byte second-stage receiver (clean-room) ---------------------
; The BootROM copies these 32 bytes to PSRAM 0xE00000 and jumps here.  We receive
; a 2048-byte stage-2 into 0xE00020 over ASC0/USIC0 and FALL THROUGH into it.
;
; Four things make a correct receiver fit in 32 bytes:
;   1. DISWDT once -> watchdog OFF for the whole session (EINIT has not run yet in
;      the BSL).  It stays off for stage-2, so no stage needs SRVWDT at all.
;   2. DPP0 = page 0x380 maps data addresses 0x0000-0x3FFF onto PSRAM 0xE00000,
;      so a store costs a 2-byte  MOVB [Rn]  instead of a 6-byte EXTS block.
;   3. The BootROM's own BSL receive loop leaves its USIC pointers in R0/R1/R2 and
;      they survive the jump into the stage:
;           R0 -> PSR   (0x4044)   protocol status, RDV flags in bits 13/14
;           R1 -> PSCR  (0x4048)   protocol status CLEAR
;           R2 -> RBUF  (0x405C)   receive buffer
;      Getting them for free is what buys room for the flag-clear below.  (R3 also
;      arrives holding 0x6000, but we load it ourselves rather than depend on it.)
;   4. PSCR <- 0x6000 after every byte.  Clearing the RDV flags is what the earlier
;      receiver was missing: without it the poll never re-arms and RBUF reads repeat
;      /desync -- the "RBUF returns garbage" wall.
;   5. CMPI1 = compare-and-increment in one 4-byte instruction, so the destination
;      pointer doubles as the loop counter (0x20..0x81F = exactly 2048 bytes).
	DISWDT				; watchdog off for the whole session
	DB	0E6h,000h,080h,003h	; MOV DPP0,#0380h -> 0x0000-0x3FFF = PSRAM 0xE00000
	MOV	R6,#0020h		; dst = 0xE00020 (stage-2 entry) and loop counter
	MOV	R3,#6000h		; RDV0|RDV1 clear-mask for PSCR
rx:	MOV	R5,[R0]			; R5 = PSR
	JNB	R5.14,rx		; no byte yet -> keep polling
	MOV	[R1],R3			; PSCR <- 0x6000: clear the receive flags
	MOVB	[R6],[R2]		; copy the RBUF byte to 0xE00020+ (DPP0-mapped)
	CMPI1	R6,#081Fh		; last byte stored?  then ++dst
	JMPR	NZ,rx			; no -> next byte; yes -> fall through into stage-2
	END
