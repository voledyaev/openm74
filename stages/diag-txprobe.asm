	CPU 80C167
;--- Bring-up probe: does uploaded code run, and is TBUF00 where we think? ----
; The very first thing to try on a board that will not talk.  Streams 0x55 out of the
; transmit buffer for ever: if the adapter sees a continuous 0x55, then the BootROM
; accepted 32 bytes, jumped into them, and the transmit address is right.  Nothing else
; can produce that pattern by accident.
;
; READ-ONLY by construction -- it never touches flash, so it cannot damage anything.
;
; TBUF00 lives at 0x204080 and is reachable through the BootROM's own DPP1 = 0x81, which
; is still loaded when the stage starts; that is why a bare 16-bit address works and no
; EXTS block is needed.  Sending 0x55 rather than 0x00 or 0xFF is deliberate: it is the
; alternating pattern, so a wrong baud shows up as a wrong byte instead of as a plausible
; run of idle bits.
	MOV	R1,#4080h		; R1 -> TBUF00
	MOV	R0,#0055h		; the byte to send: 0101 0101
tx:	MOV	[R1],R0			; write it to the transmit buffer -> K-line
	JMPR	UC,tx			; for ever; the host just stops listening
	DB	0,0,0,0,0,0,0,0		; the BootROM wants exactly 32 bytes
	DB	0,0,0,0,0,0,0,0
	DB	0,0,0,0
	END
