	CPU 80C167
;--- Bring-up probe: can the watchdog be switched off here? ------------------
; DISWDT only works before EINIT has run.  In the serial loader it has not, so the
; watchdog should be disablable -- but that is a claim about a state nobody documents
; precisely, and everything downstream depends on it: with the watchdog off, no stage has
; to spend bytes and time on SRVWDT, which is what lets the receiver fit in the 32 bytes
; the BootROM accepts and lets the dumper make one uninterrupted 832 KB pass.
;
; So: disable it, then stream 0x55 for ever WITHOUT serving it.  If the stream continues
; past the watchdog period, DISWDT took.  If it stops and the unit resets, it did not,
; and every stage needs SRVWDT after all.
;
; It streamed indefinitely on this ECU, which is why stage1recv.asm opens with DISWDT and
; why no stage in this project serves the watchdog at all.
;
; READ-ONLY: UART only.
	DISWDT				; the claim under test
	MOV	R1,#4080h		; R1 -> TBUF00
	MOV	R0,#0055h
tx:	MOV	[R1],R0			; stream, and never serve the watchdog
	JMPR	UC,tx
	DB	0,0,0,0,0,0,0,0		; the BootROM wants exactly 32 bytes
	DB	0,0,0,0,0,0,0,0
	END
