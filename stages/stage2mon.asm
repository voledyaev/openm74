	CPU 80C167
;--- Stage-2: flash monitor (clean-room) ------------------------------------
; Received into PSRAM 0xE00020 by the 32-byte stage-1, which falls through here.
; Speaks a tiny command protocol over ASC0 so the host can erase, program, read
; back and verify WITHOUT a power cycle between steps.
;
; Flash command sequences are the documented Infineon FAPI ones (XE166N UM v1.2
; ch. "Memory Organization", pp. 3-28..3-34), independently confirmed byte-for-byte
; against this ECU's own writer routines (FUN_c0c48e / FUN_c0c4ac in our dump):
;     Enter Page Mode   MOV XXAAH,XX50H · MOV PA,XXAAH
;     Load Page Word    -> XXF2H, 64 words = 128 bytes
;     Program Page      MOV XXAAH,XXA0H · MOV XX5AH,XXAAH
;     Erase Page        MOV XXAAH,XX80H · MOV XX54H,XXAAH · MOV PA,XX03H
;     Erase Sector      MOV XXAAH,XX80H · MOV XX54H,XXAAH · MOV SA,XX33H
; "XX" means the upper address bits are don't-care AND are what selects the flash
; module -- so every command cycle is issued into the TARGET's own segment (EXTS R11).
; Erased flash reads back as all-ZERO (not 0xFF), per the same manual.
;
; SAFETY: every erase/program command is gated on a segment whitelist that lives in
; THIS code (SEGMASK, patched by the host).  A stage built for 0xCC physically
; cannot touch program flash -- widening it is a deliberate, separate build.
; Reads are not gated: reading can never damage anything.
;
; Wire protocol -- marker, 6-byte frame, checksum; one status byte back:
;     A5 A5 [cmd][off_lo][off_hi][segment][count_lo][count_hi][sum16_lo][sum16_hi]
;     PROG then adds two lead bytes, 128 data bytes and a 16-bit checksum over them.
; The marker is why the frame survives the same turnaround glitch that mangles replies
; (see putst): the monitor throws away everything until a clean 0xA5, then skips any
; further 0xA5 (a command byte is never 0xA5), so it does not matter whether the first
; lead byte arrived intact.  MEASURED: a mangled READ once landed as PROGRAM and left
; the monitor waiting for 128 payload bytes that were never coming.  The checksum makes
; that failure loud and harmless instead: a frame that does not add up is never executed
; -- which matters because a mangled frame could otherwise erase the wrong sector.
;     cmd 1 PING   -> 0x55
;         2 READ   -> 0x4B then `count` bytes from segment:offset
;         3 ERASEP -> erase the 128-byte page at segment:offset      -> status
;         4 PROG   -> program the following 128 bytes there          -> status
;         5 ERASES -> erase the 4 KB sector at segment:offset        -> status
;     status: 0x4B 'K' done · 0x4E 'N' refused by the gate · 0x54 'T' flash timeout
;             0x43 'C' checksum mismatch -- nothing was executed, just send it again
; Every reply is preceded by exactly TWO 0xA5 lead bytes -- see putst for why.
;
; WHY AN OPERATION ENDED: the status byte above says whether the module went idle, which is
; not the same as whether anything happened.  After every erase and program the monitor
; copies the controller's own account -- IMB_FSR_OP and IMB_FSR_PROT -- into PSRAM at FSTAT,
; and the host fetches it with an ordinary READ.  It has to be latched rather than read live
; because "Reset to Read", which this monitor issues immediately afterwards, clears exactly
; the bits worth having.  See fin.
;
; ADDRESSING: internal calls and long jumps are ABSOLUTE (CALLA/JMPA), so this file is
; ORG'd at the address stage-1 loads it to.  The earlier all-relative build needed a
; block of trampolines to keep every CALLR inside its +/-255 byte reach, and adding the
; checksum pushed even those out of range -- a monitor that is still growing should not
; have to be laid out around a branch limit.  Absolute jumps stay inside code segment
; 0xE0 (CSP is already there), so only the 16-bit offset below has to be right.
;
; Register map, held for the whole session (byte halves exist only for R0-R7):
;     R0 -> PSR 0x4044   R1 -> PSCR 0x4048   R2 -> RBUF 0x405C   R7 -> TBUF00 0x4080
;     R3 = 0x6000 RDV clear mask      R4 = byte scratch (RH4 kept 0 to zero-extend)
;     R5 = offset   R6 = command/status   R11 = segment   R8 = count   R15 = erase code
;     R9/R10/R12/R13/R14 = scratch

DELAY	EQU	4000h		; TX pacing, host-patched to match the baud
BUF	EQU	0900h		; page buffer, DPP0-mapped -> 0xE00900 (past this stage)
FSTAT	EQU	0980h		; two words the host reads back with a plain READ -- see fin
SEGMASK	EQU	1000h		; bit n = segment 0xC0+n may be written; host-patched
SYNC	EQU	0A5h
READY	EQU	055h
ST_OK	EQU	04Bh
ST_NO	EQU	04Eh
ST_TMO	EQU	054h
ST_CKS	EQU	043h		; distinct from ST_NO so the host knows a retry is safe

	ORG	0020h		; stage-1 stores us at 0xE00020 and falls through to here

;--- entry ------------------------------------------------------------------
start:	MOV	R0,#4044h		; PSR
	MOV	R1,#4048h		; PSCR
	MOV	R2,#405Ch		; RBUF
	MOV	R7,#4080h		; TBUF00
	MOV	R3,#6000h		; RDV0|RDV1 clear mask
	MOVB	RH4,#0			; keep R4's high half clean: MOV Rn,R4 zero-extends
	MOV	R12,#0FFFFh		; 0xFFFF is not a value either status register can
	MOV	R10,#FSTAT		; hold, so it reads back as "nothing has run yet"
	MOV	[R10],R12		; rather than as whatever PSRAM powered up holding
	MOV	R10,#FSTAT+2
	MOV	[R10],R12
	MOV	R8,#8
	MOVB	RL4,#SYNC
sync1:	CALLA	UC,putb
	SUB	R8,#1
	JMPR	NZ,sync1
	MOVB	RL4,#READY		; monitor is up and listening
	CALLA	UC,putb

;--- command loop -----------------------------------------------------------
; R15 accumulates the frame checksum here; the erase handlers reuse it only after the
; frame has been checked.  getb's own scratch is R14, so it must not be used for this.
main:	CALLA	UC,getb
	CMPB	RL4,#SYNC		; throw away everything until the frame marker
	JMPA	NZ,main
mskip:	CALLA	UC,getb			; then skip further markers -- a command is never A5,
	CMPB	RL4,#SYNC		; so this absorbs a lead byte that arrived intact
	JMPA	Z,mskip
	MOV	R6,R4			; command
	MOV	R15,R4
	CALLA	UC,getb
	MOV	R5,R4			; offset low
	ADD	R15,R4
	CALLA	UC,getb
	MOV	R9,R4
	ADD	R15,R4
	SHL	R9,#8
	OR	R5,R9			; R5 = 16-bit offset within the segment
	CALLA	UC,getb
	MOV	R11,R4			; segment
	ADD	R15,R4
	CALLA	UC,getb
	MOV	R8,R4			; count low
	ADD	R15,R4
	CALLA	UC,getb
	MOV	R9,R4
	ADD	R15,R4
	SHL	R9,#8
	OR	R8,R9			; R8 = 16-bit count
	CALLA	UC,getb			; checksum, low byte
	MOV	R13,R4
	CALLA	UC,getb			; checksum, high byte
	MOV	R9,R4
	SHL	R9,#8
	OR	R13,R9
	CMP	R15,R13
	JMPA	NZ,badsum		; does not add up -> execute nothing
	CMP	R6,#1
	JMPA	Z,cping
	CMP	R6,#2
	JMPA	Z,cread
	CMP	R6,#3
	JMPA	Z,cerapg
	CMP	R6,#4
	JMPA	Z,cprog
	CMP	R6,#5
	JMPA	Z,cerasec
	CMP	R6,#6
	JMPA	Z,cbaud
	CMP	R6,#7
	JMPA	Z,ccks
refuse:	MOVB	RL4,#ST_NO		; unknown command, or gate said no
	CALLA	UC,putst
	JMPA	UC,main
badsum:	MOVB	RL4,#ST_CKS		; corrupted on the wire -- nothing ran, retry is safe
	CALLA	UC,putst
	JMPA	UC,main

cping:	MOVB	RL4,#READY
	CALLA	UC,putst
	JMPA	UC,main

;--- SET BAUD: count field carries the new PDIV -----------------------------
; The BSL autobaud tops out well below what the line can carry (38400 misses the
; measurement 2 tries in 3), but nothing stops us setting the divider ourselves once we
; are already talking.  Which register that is was found by MEASURING, not by copying
; WinFlashECU: dumping USIC0 at 9600 and again at 19200 showed exactly one register
; move, +0x1E, 130 -> 63.  So baud x (PDIV+1) is a constant, about 1.24e6 on this part.
; (WFE's 0x3C00/0x0063 are fixed configuration; the baud in their design comes from a
; value the host supplies into +0x04.  Copying their constants would have been wrong.)
;
; ORDER MATTERS: acknowledge at the OLD baud, then switch, so the host can read the ack
; and only then retune itself.  USIC lives at 0x40xx through the BootROM's DPP1=0x81,
; the same way putb reaches TBUF00, so no EXTP is needed.
cbaud:	MOVB	RL4,#ST_OK
	CALLA	UC,putst		; answered while both ends still agree
	MOV	R10,#401Eh		; USIC0 BRG, high word = PDIV
	MOV	[R10],R8		; count field carries the divider
	MOV	R10,#dlyimm+2		; and retune our own TX pacing to match:
	MOV	[R10],R5		; offset field carries the new delay
	JMPA	UC,main

;--- READ: ungated -- reading cannot damage anything ------------------------
cread:	MOVB	RL4,#ST_OK		; the lead absorbs the turnaround, so the data
	CALLA	UC,putst		; stream that follows needs no padding of its own
crl:	EXTS	R11,#1
	MOVB	RL4,[R5]		; flash byte at segment:offset
	CALLA	UC,putb
	ADD	R5,#1			; wraps inside the segment; the host chunks reads
	SUB	R8,#1
	JMPA	NZ,crl
	JMPA	UC,main

;--- CHECKSUM: two running 16-bit sums over segment:offset, count bytes -----
; Verifying a written sector by reading all 4096 bytes back costs 0.59 s of the 2.64 s that
; sector takes at 115200 -- nearly a third of a whole-image write spent shipping data we
; already have.  The ECU can add it up itself and send four bytes instead.
; (The figures here were 1.7 s of 7 s while the host was still reconfiguring its serial port
; between transfers; the ratio survived the fix, the absolute times did not.)
;
; Two accumulators, not one: a plain sum misses two errors that cancel, while the second
; accumulator depends on WHERE each byte landed, so it also catches reordering and shifts.
; Byte registers only exist for R0-R7, hence R6 as the staging register when sending --
; and note putst clobbers R13, so the status must go out BEFORE the sums are built.
ccks:	MOVB	RL4,#ST_OK
	CALLA	UC,putst		; answered first: putst uses R13, which is sum2 below
	MOV	R12,#0			; sum1: every byte
	MOV	R13,#0			; sum2: sum1 after each byte, so position matters
ckl:	EXTS	R11,#1
	MOVB	RL4,[R5]
	ADD	R12,R4
	ADD	R13,R12
	ADD	R5,#1
	SUB	R8,#1
	JMPA	NZ,ckl
	MOV	R6,R12
	MOVB	RL4,RL6
	CALLA	UC,putb
	MOVB	RL4,RH6
	CALLA	UC,putb
	MOV	R6,R13
	MOVB	RL4,RL6
	CALLA	UC,putb
	MOVB	RL4,RH6
	CALLA	UC,putb
	JMPA	UC,main

;--- ERASE ------------------------------------------------------------------
cerapg:	MOV	R15,#0003h		; Erase Page   -- 128 bytes
	JMPA	UC,cerase
cerasec:
	MOV	R15,#0033h		; Erase Sector -- 4 KB
cerase:	CALLA	UC,gate
	AND	R13,R13
	JMPA	Z,refuse
	CALLA	UC,clrst
	MOV	R10,#00AAh
	MOV	R12,#0080h
	CALLA	UC,fcmd
	MOV	R10,#0054h
	MOV	R12,#00AAh
	CALLA	UC,fcmd
	MOV	R10,R5			; the target address itself carries the code
	MOV	R12,R15
	CALLA	UC,fcmd
	CALLA	UC,waitbusy
	JMPA	UC,fin

;--- PROGRAM one 128-byte page ----------------------------------------------
; The page is a SEPARATE transfer that only starts once we have answered ST_OK, and
; that is a safety property, not a convenience: it keeps the command frame a fixed 8
; bytes -- ten on the wire with its markers -- whatever the command byte says.  When the payload rode along inside the frame,
; a command byte mangled away from 4 meant the monitor never collected those 128 bytes
; and they stayed on the wire as loose data -- data that contains 0xA5 markers and can
; therefore be re-read as a frame.  One of those false frames executing is exactly the
; accident a flash writer must not have; asking first means loose bytes never exist.
cprog:	CALLA	UC,gate			; decide before asking for the page, so a refusal
	AND	R13,R13			; never leaves a payload stranded
	JMPA	Z,refuse
	MOVB	RL4,#ST_OK		; "send the page"
	CALLA	UC,putst
	CALLA	UC,getb			; the page arrives right after we transmitted, so its
	CALLA	UC,getb			; first byte would take the turnaround hit.  Two lead
					; bytes absorb it -- and they are counted, not matched,
					; because page data may legitimately BE 0xA5, so the
					; marker search that frames a command cannot frame this.
	MOV	R10,#BUF
	MOV	R9,#128
	MOV	R15,#0			; checksum over the payload
cpr:	CALLA	UC,getb
	MOVB	[R10],RL4
	ADD	R15,R4
	ADD	R10,#1
	SUB	R9,#1
	JMPA	NZ,cpr
; 16-BIT, and that width is not cosmetic.  With an 8-bit sum a corrupted page slips
; through 1 time in 256, and a real run retries corrupted payloads hundreds of times --
; MEASURED: a full image died at sector 44 with 68 bytes wrong in one page, after ~180
; retries, which is exactly the ~50% odds an 8-bit checksum gives you over that many
; tries.  The per-sector verify caught it, but a checksum that lets bad data reach flash
; and leans on the verify is the wrong shape for a flasher.
	CALLA	UC,getb			; payload checksum, low byte
	MOV	R13,R4
	CALLA	UC,getb			; payload checksum, high byte
	MOV	R9,R4
	SHL	R9,#8
	OR	R13,R9
	CMP	R15,R13
	JMPA	NZ,badsum		; reject a corrupted page BEFORE it reaches flash
	CALLA	UC,clrst
	MOV	R10,#00AAh
	MOV	R12,#0050h		; Enter Page Mode
	CALLA	UC,fcmd
	MOV	R10,R5
	MOV	R12,#00AAh		; latch the page address
	CALLA	UC,fcmd
	CALLA	UC,waitbusy		; the ECU's own routine polls here too
	MOV	R13,#BUF
	MOV	R9,#64
cpw:	MOV	R12,[R13+]		; 64 words = one 128-byte page
	MOV	R10,#00F2h
	CALLA	UC,fcmd
	SUB	R9,#1
	JMPA	NZ,cpw
	MOV	R10,#00AAh
	MOV	R12,#00A0h		; Program Page
	CALLA	UC,fcmd
	MOV	R10,#005Ah
	MOV	R12,#00AAh
	CALLA	UC,fcmd
	CALLA	UC,waitbusy

;--- fin: latch what the controller said, THEN reset it to read mode --------
; The busy flag alone cannot tell a refused operation from one that ran and achieved
; nothing: both end with the module idle.  IMB_FSR_OP and IMB_FSR_PROT hold the reason --
; SQER (the sequence was not accepted), OPER (a previous one was cut short by a reset),
; PROER (it hit installed write protection) -- but "Reset to Read" is documented to clear
; exactly those bits, and rstread is the very next thing this monitor does.  So they are
; copied into PSRAM first, where the host can fetch them with an ordinary READ; no new
; command, no change to any reply, and the write path keeps the shape it was proven in.
; (XC2000 System Units UM V1.0, Table 3-7: FF'FF08H and FF'FF0AH.)
fin:	MOV	R6,R12			; keep the status -- rstread reuses R12
	MOV	R10,#3F08h		; IMB_FSR_OP:   PROG ERASE POWER MAR SQER OPER
	EXTP	#3FFh,#1
	MOV	R13,[R10]
	MOV	R10,#FSTAT
	MOV	[R10],R13
	MOV	R10,#3F0Ah		; IMB_FSR_PROT: PROIN PROINER RPRODIS WPRODIS PROER
	EXTP	#3FFh,#1
	MOV	R13,[R10]
	MOV	R10,#FSTAT+2
	MOV	[R10],R13
	CALLA	UC,rstread
	MOVB	RL4,#ST_OK
	CMP	R6,#0
	JMPR	Z,fin1
	MOVB	RL4,#ST_TMO
fin1:	CALLA	UC,putst
	JMPA	UC,main

;--- getb: block until a byte arrives; return it in RL4 ---------------------
getb:	MOV	R14,[R0]		; PSR
	JNB	R14.14,getb		; nothing yet -> keep polling
	MOV	[R1],R3			; PSCR <- 0x6000: re-arm the receive flags
	MOVB	RL4,[R2]		; take the byte out of RBUF
	RET

;--- putb: transmit RL4, pace it, then swallow our own echo -----------------
; K is half-duplex and the L9637D feeds RX from the bus, so the ECU hears every
; byte it sends.  Un-drained, that echo would come back as the next "command".
putb:	MOVB	[R7],RL4		; TBUF00 -> K-line
dlyimm:	MOV	R9,#DELAY		; SET BAUD rewrites this immediate in place (we are
					; in RAM); without that, retuning the line would leave
					; the pacing tuned to the old rate and reads would not
					; speed up at all -- and a pacing shorter than the byte
					; takes on the wire would overrun the transmitter.
putd:	SUB	R9,#1
	JMPR	NZ,putd			; ~1.6x the time the byte occupies on the wire
	MOV	[R1],R3			; clear the flags our own transmission raised
	MOV	R9,[R2]			; and drop the echoed byte
	RET

;--- putst: transmit RL4 as a reply, behind two throwaway lead bytes --------
; MEASURED ON THE BENCH: the first byte the ECU transmits after it has been receiving
; comes out corrupted -- the K-line turns around too slowly, its start bit is missed,
; and the receiver re-triggers two bit-times late.  It reproduces exactly: 0xA5 -> 0xE9,
; 0x55 -> 0xD5, 0x4B -> 0xE9.  Everything after that byte is exact (a READ answered
; "e9 fa c0 a2 02" -- status mangled, all four flash bytes perfect).  So every reply
; leads with two sync bytes: the glitch lands on the first, the second proves the line
; settled, and the status arrives clean.  Fixed count, so the host never has to guess.
putst:	MOV	R13,R4			; keep the status (RH4 stays 0, callers rely on it)
	MOVB	RL4,#SYNC
	CALLA	UC,putb
	CALLA	UC,putb
	MOV	R4,R13
	JMPA	UC,putb			; tail call: putb's RET returns to OUR caller

;--- fcmd: flash command cycle -- write R12 to segment R11 : offset R10 -----
fcmd:	EXTS	R11,#1			; upper address bits = the module being addressed
	MOV	[R10],R12
	RET

;--- gate: R13 <- 0 if segment R11 is not in SEGMASK (erase/program refused) -
gate:	MOV	R12,R11
	SUB	R12,#00C0h		; index of the segment within the flash
	MOV	R13,R12
	AND	R13,#0FFF0h		; outside 0xC0..0xCF -> not flash at all
	JMPR	NZ,gateno
	MOV	R13,#1
	SHL	R13,R12
	AND	R13,#SEGMASK		; nonzero -> this segment is writable
	RET
gateno:	MOV	R13,#0
	RET

;--- waitbusy: poll FFFF06 until the module goes idle; R12 = 0 ok / 1 timeout
; FFFF06 carries one busy bit per flash module, so testing the low nibble covers
; whichever module we addressed.  Clobbers R8 -- only ever called once `count`
; has been consumed.  The timeout is deliberately far longer than needed: a sector erase
; measures 20 ms, and the loop is sized so that a slow or unhealthy module still gets its
; chance to finish rather than being called a timeout.
waitbusy:
	MOV	R8,#0200h
	MOV	R10,#3F06h		; EXTP #3FFh -> 0xFFC000 + 0x3F06 = 0xFFFF06
wbo:	MOV	R14,#0FFFFh
wbi:	EXTP	#3FFh,#1
	MOV	R12,[R10]
	AND	R12,#000Fh
	JMPR	Z,wbok
	SUB	R14,#1
	JMPR	NZ,wbi
	SUB	R8,#1
	JMPR	NZ,wbo
	MOV	R12,#1			; flash never went idle
	RET
wbok:	MOV	R12,#0
	RET

;--- clrst / rstread: documented Clear Status and Reset-to-Read -------------
; The ECU's own routines skip both (its app never reads back what it just wrote);
; we issue them so a stale error flag cannot block us and so our READ command
; sees data rather than command state.  Both tail-call fcmd.
clrst:	MOV	R10,#00AAh
	MOV	R12,#00F5h
	JMPA	UC,fcmd			; fcmd's RET returns to OUR caller
rstread:
	MOV	R10,#00AAh
	MOV	R12,#00F0h
	JMPA	UC,fcmd
	END
