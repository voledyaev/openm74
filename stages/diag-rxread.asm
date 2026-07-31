	CPU 80C167
;--- Bring-up probe: does the receive buffer return the byte that arrived? ---
; diag-echo answers "does anything arrive"; this answers the next question, which caught
; a real bug: the byte the buffer hands back may not be the byte that came in.  It waits
; for ONE byte and then streams that byte for ever, with no further receiving.  Send 0x5A
; and a clean unbroken run of 0x5A means the read is correct.  Anything else -- a shifted
; value, an alternating pattern, a run of some other constant -- says the receive buffer
; is being read wrongly or is colliding with the transmit side.
;
; Separating this from diag-echo matters because echoing byte-by-byte interleaves a read
; and a write on every byte, so a collision between the two looks exactly like a bad read.
; Streaming after a single read removes the write from the loop entirely.
;
; READ-ONLY: UART only.
	MOV	R1,#4080h		; R1 -> TBUF00
rx:	SRVWDT
	MOV	R5,4058h		; protocol status
	AND	R5,#6000h		; RDV0|RDV1
	JMPR	Z,rx			; nothing yet
	MOV	R2,405Ch		; the one byte we will repeat for ever
tx:	SRVWDT
	MOV	[R1],R2
	JMPR	UC,tx
	DB	0,0			; the BootROM wants exactly 32 bytes
	END
