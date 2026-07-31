	CPU 80C167
;--- Bring-up probe: is the RECEIVE path alive? ------------------------------
; Receives one byte and sends it straight back, for ever.  A byte sent to an ECU running
; this comes back TWICE: once because K-line is half-duplex and the transceiver echoes
; everything on the bus, and once from this stub.  One echo means the wiring is fine and
; the stub is not receiving; two means the receive path works end to end.
;
; That distinction is the whole point.  The half-duplex echo arrives even when the ECU is
; dead, so "I see my own bytes" proves nothing on its own -- which is a trap this bench
; fell into before the second echo was there to separate the two cases.
;
; READ-ONLY: it touches the UART and nothing else.
;
; NOTE, for anyone reading this as an example: it polls the status word and never clears
; the receive flags afterwards, so it re-reads the same byte.  Harmless here, where the
; question is only "does anything arrive at all", and fatal in a real receiver -- see the
; comment at the top of stage1recv.asm, where that omission was the bug.
	MOV	R1,#4080h		; R1 -> TBUF00
rx:	SRVWDT
	MOV	R5,4058h		; the protocol status word
	AND	R5,#6000h		; RDV0|RDV1: has a byte arrived?
	JMPR	Z,rx			; no -> keep polling
	MOV	R5,405Ch		; yes -> take it out of the receive buffer
	MOV	[R1],R5			; and send it straight back
	JMPR	UC,rx
	DB	0,0,0,0,0,0		; the BootROM wants exactly 32 bytes
	END
