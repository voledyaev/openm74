	CPU 80C167
;--- Bring-up probe: is flash addressing right? -----------------------------
; The second thing to try, once diag-txprobe has shown that uploaded code runs.  Streams
; flash segment 0xC0 from 0xC00000 upward, one byte at a time, for ever.  Compare what
; arrives against a known image of the same ECU: if it matches, the segment override, the
; read and the transmit path are all correct, and any remaining trouble is in the
; two-stage handoff rather than in the addressing.
;
; READ-ONLY by construction -- it reads flash and writes the UART, nothing else, so it
; cannot damage the unit whatever the addressing turns out to be.  That is exactly why
; it is worth running before anything that erases.
;
; It serves the watchdog on every byte (SRVWDT) because this stub predates the discovery
; that DISWDT works in the BSL; leaving it in costs 4 bytes and makes the probe safe to
; run whatever state the part is in.
	MOV	R1,#4080h		; R1 -> TBUF00, via the BootROM's DPP1 = 0x81
	MOV	R0,#0			; the byte read out of flash goes here
	MOV	R5,#0			; offset 0x0000..0xFFFF inside the segment
bl:	SRVWDT				; keep the watchdog quiet
	EXTS	#0C0h,#1		; the NEXT access uses flash segment 0xC0
	MOVB	RL0,[R5]		; read 0xC0:R5
	MOV	[R1],R0			; transmit it (only the low byte reaches the wire)
	MOV	R2,#4000h		; --- pacing, ~1.6x the time a byte takes at 9600 ---
dl:	SUB	R2,#1
	JMPR	NZ,dl
	ADD	R5,#1			; ++offset; wraps at the end of the segment
	JMPR	UC,bl
	END
