/*
 * nota.h - shared definitions for NOTA (AArch64 assembly).
 *
 * NOTA is an Android notes application whose entire application logic is
 * written in AArch64 assembly. This header describes the ABIs it consumes:
 * bionic/NDK C library entry points, android_native_app_glue, the NDK
 * platform headers (native_activity.h, input.h, looper.h) and OpenGL ES 2/3.
 *
 * Offsets below are normative: they are asserted at assembly time with
 * .equ/.if blocks so a layout mistake fails the build, not the device.
 */
#ifndef NOTA_H
#define NOTA_H

/* ------------------------------------------------------------------ ELF ---- */
#define L_GLOBAL  0x10          /* STB_GLOBAL << 4 | STT_NOTYPE */
#define L_FUNC    0x12          /* STB_GLOBAL << 4 | STT_FUNC   */
#define L_OBJECT  0x11          /* STB_GLOBAL << 4 | STT_OBJECT */

/* AArch64 relocation for "adrp/add" style symbol address loads. */
#define ADRP(sym) adrp x16, sym ; add x16, x16, :lo12:sym

/* ------------------------------------------------------- ANativeActivity ---
 * struct ANativeActivity (android/native_activity.h), LP64:
 *   0x00 callbacks          (ANativeActivityCallbacks*)
 *   0x08 vm                 (JavaVM*)
 *   0x10 env                (JNIEnv*)
 *   0x18 clazz              (jobject, the NativeActivity instance)
 *   0x20 internalDataPath   (const char*)
 *   0x28 externalDataPath   (const char*)
 *   0x30 sdkVersion         (int32)
 *   0x38 instance           (void*)
 *   0x40 assetManager       (AAssetManager*)
 *   0x48 obbPath            (const char*)
 */
#define NA_CALLBACKS      0x00
#define NA_VM             0x08
#define NA_ENV            0x10
#define NA_CLAZZ          0x18
#define NA_INTERNAL_PATH  0x20
#define NA_EXTERNAL_PATH  0x28
#define NA_SDK            0x30
#define NA_INSTANCE       0x38
#define NA_ASSET_MGR      0x40
#define NA_OBB_PATH       0x48
#define NA_SIZE           0x50

/* ANativeActivityCallbacks, 19 function pointers:
 *  0x00 onStart          0x08 onResume         0x10 onSaveInstanceState
 *  0x18 onPause          0x20 onStop           0x28 onDestroy
 *  0x30 onWindowFocusChanged                    0x38 onNativeWindowCreated
 *  0x40 onNativeWindowResized                   0x48 onNativeWindowRedrawNeeded
 *  0x50 onNativeWindowDestroyed                 0x58 onInputQueueCreated
 *  0x60 onInputQueueDestroyed                   0x68 onContentRectChanged
 *  0x70 onConfigurationChanged                  0x78 onLowMemory
 */
#define CB_START          0x00
#define CB_RESUME         0x08
#define CB_SAVE_STATE     0x10
#define CB_PAUSE          0x18
#define CB_STOP           0x20
#define CB_DESTROY        0x28
#define CB_FOCUS          0x30
#define CB_WIN_CREATED    0x38
#define CB_WIN_RESIZED    0x40
#define CB_WIN_REDRAW     0x48
#define CB_WIN_DESTROYED  0x50
#define CB_QUEUE_CREATED  0x58
#define CB_QUEUE_DESTROYED 0x60
#define CB_CONTENT_RECT   0x68
#define CB_CONFIG         0x70
#define CB_LOW_MEMORY     0x78
#define CB_COUNT          16

/* ------------------------------------------------- android_app (glue) ------
 * android_native_app_glue.h, LP64:
 *   0x00 userData                0x08 onAppCmd
 *   0x10 onInputEvent            0x18 activity
 *   0x20 config                  0x24..0x2b savedStateSize (size_t at 0x28)
 *   0x30 savedState
 *   0x38 looper                 0x40 inputQueue
 *   0x48 pendingInputQueue      0x50 window
 *   0x58 pendingWindow          0x60 running
 *   0x64 stateSaved             0x68 destroyed
 *   0x6c redrawNeeded           0x70 pendingContentRect
 *                               0x80 contentRect
 */
#define APP_USER_DATA     0x00
#define APP_ON_CMD        0x08
#define APP_ON_INPUT      0x10
#define APP_ACTIVITY      0x18
#define APP_CONFIG        0x20
#define APP_SAVED_SIZE    0x28
#define APP_SAVED_STATE   0x30
#define APP_LOOPER        0x38
#define APP_INPUT_QUEUE   0x40
#define APP_PEND_QUEUE    0x48
#define APP_WINDOW        0x50
#define APP_PEND_WINDOW   0x58
#define APP_RUNNING       0x60
#define APP_STATE_SAVED   0x64
#define APP_DESTROYED     0x68
#define APP_REDRAW        0x6c
#define APP_PEND_RECT     0x70
#define APP_CONTENT_RECT  0x80
#define APP_SIZE          0x90

/* Command values delivered to onAppCmd (app_glue) */
#define CMD_INIT_WINDOW         1
#define CMD_TERM_WINDOW         2
#define CMD_INPUT_CHANGED       3
#define CMD_CONFIG_CHANGED      4
#define CMD_LOW_MEMORY          5
#define CMD_START               6
#define CMD_RESUME              7
#define CMD_SAVE_STATE          8
#define CMD_PAUSE               9
#define CMD_STOP                10
#define CMD_DESTROY             11
#define CMD_WINDOW_REDRAW_NEEDED 12
#define CMD_CONTENT_RECT_CHANGED 13

/* ------------------------------------------------------------ AInputEvent --
 * Events are opaque; all access goes through NDK accessors. The values used:
 */
#define AINPUT_EVENT_TYPE_KEY     1
#define AINPUT_EVENT_TYPE_MOTION  2

#define AKEY_EVENT_ACTION_DOWN    0
#define AKEY_EVENT_ACTION_UP      1
#define AKEY_EVENT_ACTION_MULTIPLE 2

#define AMOTION_EVENT_ACTION_DOWN          0
#define AMOTION_EVENT_ACTION_UP            1
#define AMOTION_EVENT_ACTION_MOVE          2
#define AMOTION_EVENT_ACTION_CANCEL        3
#define AMOTION_EVENT_ACTION_POINTER_DOWN  5
#define AMOTION_EVENT_ACTION_POINTER_UP    6
#define AMOTION_EVENT_ACTION_MASK          0xff
#define AMOTION_EVENT_ACTION_POINTER_INDEX_SHIFT 8

/* Key codes used by the editor (android/keycodes.h) */
#define AKEYCODE_UNKNOWN     0
#define AKEYCODE_BACK        4
#define AKEYCODE_0           7
#define AKEYCODE_9           16
#define AKEYCODE_A           29
#define AKEYCODE_Z           54
#define AKEYCODE_DPAD_UP     19
#define AKEYCODE_DPAD_DOWN   20
#define AKEYCODE_DPAD_LEFT   21
#define AKEYCODE_DPAD_RIGHT  22
#define AKEYCODE_SHIFT_LEFT  59
#define AKEYCODE_TAB         61
#define AKEYCODE_SPACE       62
#define AKEYCODE_ENTER       66
#define AKEYCODE_DEL         67
#define AKEYCODE_ESCAPE      111
#define AKEYCODE_MOVE_HOME   122
#define AKEYCODE_MOVE_END    123
#define AKEYCODE_FORWARD_DEL 112

/* Meta state */
#define AMETA_SHIFT_ON    0x01
#define AMETA_CTRL_ON     0x1000
#define AMETA_ALT_ON      0x02

/* --------------------------------------------------------------- Colors ---- */
#define COL_BG         0xFF14161A
#define COL_PANEL      0xFF1B1E22
#define COL_PANEL_HI   0xFF23272D
#define COL_TEXT       0xFFE6E8EB
#define COL_TEXT_DIM   0xFF8A9099
#define COL_ACCENT     0xFFF2C94C
#define COL_ACCENT_DIM 0xFF8A7433
#define COL_CURSOR     0xFFF2C94C
#define COL_SELECT     0x553B6FB5
#define COL_LINE       0xFF2A2F36
#define COL_TAG        0xFF3A7D5C
#define COL_DANGER     0xFFB5443B

/* --------------------------------------------------- editor document -------
 * struct edit_doc layout, shared by edit.S, store.S and the UI. The offsets
 * are asserted by the tests, so a layout change must be made here first.
 */
#define DOC_BUF       0x00
#define DOC_CAP       0x08
#define DOC_LEN       0x10
#define DOC_CPS       0x18
#define DOC_CURSOR    0x20
#define DOC_ANCH      0x28
#define DOC_DIRTY     0x30
#define DOC_SELECTING 0x38
#define DOC_BURST     0x40   /* end offset of the live typing burst, or -1 */
#define DOC_UNC       0x48
#define DOC_UNP       0x50
#define DOC_UENT      0x58
#define DOC_UNMAX     0x60
#define DOC_UARENA    0x68
#define DOC_UACAP     0x70
#define DOC_UAN       0x78
#define DOC_GOALCOL   0x80   /* remembered column for vertical moves          */
#define DOC_GOALSET   0x88   /* cursor offset the goal was measured at, or -1  */
#define EDIT_DOC_SIZE 0x90

/* undo entry layout (DOC_UENT points at an array of these) */
#define UNDO_OFF      0x00
#define UNDO_INSLEN   0x08
#define UNDO_DELLEN   0x10
#define UNDO_INSPTR   0x18
#define UNDO_DELPTR   0x20
#define UNDO_SIZE     0x28

/* editor command results */
#define EDIT_OK         0
#define EDIT_ERR_FULL  -1
#define EDIT_ERR_RANGE -2
#define EDIT_ERR_CP    -3

/* ---------------------------------------------------------- notes/records ---
 * struct note: the editor/store working representation. Title and body point
 * into a per-note arena so the struct stays fixed-size.
 */
#define NOTE_ID        0x00
#define NOTE_FLAGS     0x04
#define NOTE_FOLDER    0x08
#define NOTE_TAGS      0x0C   /* u32 count                         */
#define NOTE_CREATED   0x10
#define NOTE_UPDATED   0x18
#define NOTE_TITLE_LEN 0x20   /* u32                               */
#define NOTE_BODY_LEN  0x24   /* u32                               */
#define NOTE_TITLE_PTR 0x28
#define NOTE_BODY_PTR  0x30
#define NOTE_TAG_PTR   0x38   /* u32 array                         */
#define NOTE_SIZE      0x40

#define NOTE_HDR_SIZE  0x28   /* bytes of fixed fields in a record   */
#define NOTE_FLAG_PINNED  0x01
#define NOTE_FLAG_TRASHED 0x02

/* folder record in memory */
#define FOLDER_ID      0x00
#define FOLDER_PARENT  0x04
#define FOLDER_NAME_PTR 0x08
#define FOLDER_SIZE    0x10

/* tag record in memory */
#define TAG_ID         0x00
#define TAG_NAME_PTR   0x08
#define TAG_SIZE       0x10

/* store results */
#define STORE_OK            0
#define STORE_ERR_TRUNCATED -1
#define STORE_ERR_CORRUPT   -2
#define STORE_ERR_NOSPACE   -3
#define STORE_ERR_IO        -4
#define STORE_ERR_FULL      -5

/* ---------------------------------------------------------------- Sizes ---- */
#define MAX_NOTES        512
#define MAX_FOLDERS      64
#define MAX_TAGS         128
#define MAX_NOTE_BYTES   65536
#define MAX_TAG_LEN      24
#define MAX_TITLE_LEN    96
#define MAX_UNDO         128
#define MAX_PATH         512
#define MAX_SEARCH       128

/* ------------------------------------------------------------- GL limits ---- */
#define GL_TRIANGLES     0x0004
#define GL_TEXTURE_2D    0x0DE1
#define GL_TEXTURE0      0x84C0
#define GL_BLEND         0x0BE2
#define GL_SRC_ALPHA     0x0302
#define GL_ONE_MINUS_SRC_ALPHA 0x0303
#define GL_ARRAY_BUFFER  0x8892
#define GL_STREAM_DRAW   0x88E0
#define GL_STATIC_DRAW   0x88E4
#define GL_VERTEX_SHADER 0x8B31
#define GL_FRAGMENT_SHADER 0x8B30
#define GL_COMPILE_STATUS 0x8B81
#define GL_LINK_STATUS   0x8B82
#define GL_FLOAT         0x1406
#define GL_UNSIGNED_SHORT 0x1403
#define GL_UNSIGNED_BYTE 0x1401
#define GL_RGBA          0x1908
#define GL_RGBA8         0x8058
#define GL_LINEAR        0x2601
#define GL_CLAMP_TO_EDGE 0x812F
#define GL_TEXTURE_MIN_FILTER 0x2801
#define GL_TEXTURE_MAG_FILTER 0x2800
#define GL_TEXTURE_WRAP_S 0x2802
#define GL_TEXTURE_WRAP_T 0x2803
#define GL_COLOR_ATTACHMENT0 0x8CE0
#define GL_FRAMEBUFFER   0x8D40
#define GL_TEXTURE_SWIZZLE_RGBA 0x8E46

/* ------------------------------------------------- renderer state block ----
 * One static struct holds what the renderer needs between frames, so init
 * resolves each name once (glGetAttribLocation is a string lookup) and the
 * per-frame path is arithmetic only.
 */
#define GL_W          0x00   /* long: surface width in pixels                */
#define GL_H          0x08   /* long: surface height in pixels               */
#define GL_PROG       0x10   /* unsigned: program object                     */
#define GL_TEX        0x18   /* unsigned: atlas texture                      */
#define GL_VBO        0x20   /* unsigned: vertex buffer                      */
#define GL_NVERTS     0x28   /* unsigned: vertices queued this frame         */
#define GL_A_POS      0x2C   /* int: attribute locations                     */
#define GL_A_UV       0x30
#define GL_A_COL      0x34
#define GL_U_PROJ     0x38   /* int: uniform locations                       */
#define GL_U_TEX      0x3C
#define GL_CR         0x40   /* float: current colour, straight alpha        */
#define GL_CG         0x44
#define GL_CB         0x48
#define GL_CA         0x4C
#define GL_PROJ_W     0x50   /* float: viewport width, for u_proj            */
#define GL_PROJ_H     0x54   /* float: viewport height                       */
#define GL_STATE_SIZE 0x58

#define GL_MAX_VERTS  4096
/* x, y, u, v, r, g, b, a - eight float32 per vertex. The attribute
 * strides below are expressed in floats (glVertexAttribPointer takes a byte
 * stride, which is this * 4), so this must stay in step with the eight stores
 * nota_gl_quad makes per vertex. */
#define GL_VSTRIDE    32
#define GL_LUMINANCE_ALPHA 0x190A
#define GL_ALPHA           0x1906
#define GL_COLOR_BUFFER_BIT 0x4000

#endif /* NOTA_H */
