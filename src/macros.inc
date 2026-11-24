/* macros.inc - assembler helpers used by the UI sources.
 *
 * A 32-bit colour literal (0xFFRRGGBB) is never a legal single `mov`
 * immediate, and spelling movz/movk by hand at every call site invites a typo
 * that shifts a colour by a nibble. These wrap it.
 */

/* mov32 reg, value - load any 32-bit constant into a w register. */
.macro mov32 reg, val
    movz    \reg, #((\val) & 0xFFFF)
    movk    \reg, #(((\val) >> 16) & 0xFFFF), lsl #16
.endm

/* mov64 reg, value - load any 64-bit constant. */
.macro mov64 reg, val
    movz    \reg, #((\val) & 0xFFFF)
    movk    \reg, #(((\val) >> 16) & 0xFFFF), lsl #16
    movk    \reg, #(((\val) >> 32) & 0xFFFF), lsl #32
    movk    \reg, #(((\val) >> 48) & 0xFFFF), lsl #48
.endm
