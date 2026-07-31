	CPU 80C167
;--- Stage-2: one-shot 832 KB flash dumper (clean-room) ----------------------
; Received into PSRAM 0xE00020 by the 32-byte stage-1, which falls through to here.
; Streams flash 0xC00000..0xCCFFFF (segments 0xC0..0xCC, 13 x 64KB) out of ASC0
; TBUF00, then spins quietly.  READ-ONLY: it only reads flash and writes the UART,
; so it cannot brick the ECU.
;
; No SRVWDT anywhere -- stage-1's DISWDT already killed the watchdog for the whole
; session, which is exactly what makes a single uninterrupted 832 KB pass possible
; (the old per-segment dumper had to let the watchdog reset the MCU between segments).
;
; Stage-2 has 2 KB of room, so each segment gets its own unrolled loop with an
; IMMEDIATE  EXTS #seg,#1  -- the addressing form already proven byte-perfect on this
; ECU.  Nothing here uses an instruction form we have not already run on hardware.

; Pacing loop, proven at 9600 baud with zero drops (0x4000 -> 1.64 ms/byte, ~1.6x the
; 1.04 ms a byte occupies on the wire).  The host PATCHES this immediate at both sites
; when it runs the line faster -- see build_stage2()/pacing_delay() in klinebsl.py --
; so keep it inversely proportional to the baud or a fast line just idles here.
DELAY	EQU	4000h

; Branch targets are written as $-N rather than labels: this ASL build has no
; macro-local symbols, and a label re-defined by all 13 expansions is a trap.
dumpseg	MACRO	seg			; stream one 64KB flash segment
	MOV	R5,#0			; R5 = offset 0..FFFF inside the segment
	EXTS	#seg,#1			; (bL) the next access uses flash segment `seg`
	MOVB	RL0,[R5]		; read flash byte  seg:R5
	MOVB	[R1],RL0		; transmit it (byte write to TBUF00)
	MOV	R2,#DELAY		; --- pacing delay ---
	SUB	R2,#1			; (dL)
	JMPR	NZ,$-2			; -> dL
	ADD	R5,#1			; ++offset ; ADD sets Z when it wraps 0xFFFF->0
	JMPR	NZ,$-18			; -> bL ; keep going until the segment is done
	ENDM

	MOV	R1,#4080h		; R1 -> TBUF00 (reachable via the BootROM's DPP1=0x81)
; --- sync preamble ---------------------------------------------------------
; Measured on the bench: the FIRST byte stage-2 transmits comes out corrupted
; (0xFA read back as 0xFE, one bit) because the K-line transceiver is still
; turning around from receive to transmit.  Everything after it is byte-perfect.
; So we burn the turnaround on 8 sync bytes instead of on flash data: the host
; expects PREAMBLE bytes at a fixed offset and tolerates a glitch in the first.
	MOV	R0,#00A5h		; sync byte
	MOV	R4,#8			; how many
	MOVB	[R1],RL0		; (pL) transmit one sync byte
	MOV	R2,#DELAY		; --- pacing delay ---
	SUB	R2,#1			; (pD)
	JMPR	NZ,$-2			; -> pD
	SUB	R4,#1
	JMPR	NZ,$-12			; -> pL
; --- the dump itself -------------------------------------------------------
	dumpseg	0C0h
	dumpseg	0C1h
	dumpseg	0C2h
	dumpseg	0C3h
	dumpseg	0C4h
	dumpseg	0C5h
	dumpseg	0C6h
	dumpseg	0C7h
	dumpseg	0C8h
	dumpseg	0C9h
	dumpseg	0CAh
	dumpseg	0CBh
	dumpseg	0CCh
done:	JMPR	done			; quiet spin: no TX, no reset -- host just stops reading
	END
