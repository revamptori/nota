/* glue.h - the handful of NDK ABI declarations NOTA needs.
 *
 * NOTA deliberately does not include the NDK headers. The toolchain must build
 * with nothing but a cross-compiler, and every offset here is already the
 * normative one in src/nota.h. Declaring the ABI ourselves means a wrong
 * layout fails a compile-time assert in this file rather than a device.
 */
#ifndef NOTA_GLUE_H
#define NOTA_GLUE_H

#include <stdint.h>
#include <stddef.h>

/* ---------------------------------------------------------------- android -- */
typedef struct ANativeActivity ANativeActivity;
typedef struct ANativeWindow ANativeWindow;
typedef struct AAssetManager AAssetManager;
typedef struct AAsset AAsset;
typedef struct ALooper ALooper;
typedef struct AInputQueue AInputQueue;
typedef union AInputEvent AInputEvent;

/* ARect must be complete before it is used by value in android_app. */
struct ARect { int32_t left, top, right, bottom; };

struct ANativeActivityCallbacks {
    void (*onStart)(ANativeActivity *);
    void (*onResume)(ANativeActivity *);
    void (*onSaveInstanceState)(ANativeActivity *, void *, size_t);
    void (*onPause)(ANativeActivity *);
    void (*onStop)(ANativeActivity *);
    void (*onDestroy)(ANativeActivity *);
    void (*onWindowFocusChanged)(ANativeActivity *, int);
    void (*onNativeWindowCreated)(ANativeActivity *, ANativeWindow *);
    void (*onNativeWindowResized)(ANativeActivity *, ANativeWindow *);
    void (*onNativeWindowRedrawNeeded)(ANativeActivity *, ANativeWindow *);
    void (*onNativeWindowDestroyed)(ANativeActivity *, ANativeWindow *);
    void (*onInputQueueCreated)(ANativeActivity *, AInputQueue *);
    void (*onInputQueueDestroyed)(ANativeActivity *, AInputQueue *);
    void (*onContentRectChanged)(ANativeActivity *, const struct ARect *);
    void (*onConfigurationChanged)(ANativeActivity *);
    void (*onLowMemory)(ANativeActivity *);
};

#define NOTA_CB_COUNT 16

struct ANativeActivity {
    struct ANativeActivityCallbacks *callbacks;   /* 0x00 */
    void *vm;                                     /* 0x08 */
    void *env;                                    /* 0x10 */
    void *clazz;                                  /* 0x18 */
    const char *internalDataPath;                 /* 0x20 */
    const char *externalDataPath;                 /* 0x28 */
    int32_t sdkVersion;                           /* 0x30 */
    void *instance;                               /* 0x38 */
    AAssetManager *assetManager;                  /* 0x40 */
    const char *obbPath;                          /* 0x48 */
};

/* android_native_app_glue.h */
struct android_app {
    void *userData;                               /* 0x00 */
    void (*onAppCmd)(struct android_app *, int32_t);          /* 0x08 */
    int32_t (*onInputEvent)(struct android_app *, AInputEvent *); /* 0x10 */
    ANativeActivity *activity;                    /* 0x18 */
    void *config;                                 /* 0x20 */
    void *savedState;                             /* 0x30 */
    size_t savedStateSize;                        /* 0x28 */
    void *looper;                                 /* 0x38 */
    AInputQueue *inputQueue;                      /* 0x40 */
    AInputQueue *pendingInputQueue;               /* 0x48 */
    ANativeWindow *window;                        /* 0x50 */
    ANativeWindow *pendingWindow;                 /* 0x58 */
    int running;                                  /* 0x60 */
    int stateSaved;                               /* 0x64 */
    int destroyed;                                /* 0x68 */
    int redrawNeeded;                             /* 0x6c */
    struct ARect pendingContentRect;              /* 0x70 */
    struct ARect contentRect;                     /* 0x80 */
};

/* Compile-time proof that this file and src/nota.h agree. A mismatch shows up
 * as a negative array size, i.e. at build time. */
#define NOTA_OFF(expr, off) \
    typedef char nota_off_##__LINE__[(expr) == (off) ? 1 : -1]
NOTA_OFF(offsetof(struct ANativeActivity, internalDataPath), 0x20);
NOTA_OFF(offsetof(struct ANativeActivity, sdkVersion), 0x30);
NOTA_OFF(offsetof(struct ANativeActivity, assetManager), 0x40);
NOTA_OFF(offsetof(struct ANativeActivity, obbPath), 0x48);
NOTA_OFF(offsetof(struct android_app, onInputEvent), 0x10);
NOTA_OFF(offsetof(struct android_app, inputQueue), 0x40);
NOTA_OFF(offsetof(struct android_app, window), 0x50);
NOTA_OFF(offsetof(struct android_app, running), 0x60);
NOTA_OFF(offsetof(struct android_app, contentRect), 0x80);

/* App commands (app_glue's APP_CMD_*) */
#define NOTA_CMD_INIT_WINDOW          1
#define NOTA_CMD_TERM_WINDOW          2
#define NOTA_CMD_INPUT_CHANGED        3
#define NOTA_CMD_CONFIG_CHANGED       4
#define NOTA_CMD_LOW_MEMORY           5
#define NOTA_CMD_START                6
#define NOTA_CMD_RESUME               7
#define NOTA_CMD_SAVE_STATE           8
#define NOTA_CMD_PAUSE                9
#define NOTA_CMD_STOP                 10
#define NOTA_CMD_DESTROY              11
#define NOTA_CMD_WINDOW_REDRAW_NEEDED 12
#define NOTA_CMD_CONTENT_RECT_CHANGED 13

/* looper.h */
#define NOTA_LOOPER_ID_MAIN 1
#define NOTA_LOOPER_ID_INPUT 2
#define NOTA_LOOPER_ID_USER 3

/* native_window.h */
int32_t ANativeWindow_getWidth(ANativeWindow *);
int32_t ANativeWindow_getHeight(ANativeWindow *);
int32_t ANativeWindow_setBuffersGeometry(ANativeWindow *, int32_t, int32_t,
                                         int32_t);
void ANativeWindow_acquire(ANativeWindow *);
void ANativeWindow_release(ANativeWindow *);

/* looper */
ALooper *ALooper_prepare(int opts);
int ALooper_pollOnce(int timeoutMillis, int *outFd, int *outEvents,
                     void **outData);
void ALooper_wake(ALooper *);
int ALooper_addFd(ALooper *, int fd, int ident, int events,
                  int (*callback)(int, int, void *), void *data);
int ALooper_removeFd(ALooper *, int fd);

/* input */
void AInputQueue_attachLooper(AInputQueue *, ALooper *, int ident,
                              int (*callback)(int, int, void *), void *data);
void AInputQueue_detachLooper(AInputQueue *);
int32_t AInputQueue_getEvent(AInputQueue *, AInputEvent **);
int32_t AInputQueue_preDispatchEvent(AInputQueue *, AInputEvent *);
void AInputQueue_finishEvent(AInputQueue *, AInputEvent *, int handled);
int32_t AInputEvent_getType(const AInputEvent *);
int32_t AInputEvent_getSource(const AInputEvent *);
int32_t AKeyEvent_getAction(const AInputEvent *);
int32_t AKeyEvent_getKeyCode(const AInputEvent *);
int32_t AKeyEvent_getMetaState(const AInputEvent *);
int32_t AKeyEvent_getRepeatCount(const AInputEvent *);
int32_t AMotionEvent_getAction(const AInputEvent *);
float AMotionEvent_getX(const AInputEvent *, size_t pointer_index);
float AMotionEvent_getY(const AInputEvent *, size_t pointer_index);
size_t AMotionEvent_getPointerCount(const AInputEvent *);
int64_t AMotionEvent_getEventTime(const AInputEvent *);

/* activity */
void ANativeActivity_finish(ANativeActivity *);
void ANativeActivity_showSoftInput(ANativeActivity *, int32_t flags);
void ANativeActivity_hideSoftInput(ANativeActivity *, int32_t flags);
void ANativeActivity_setWindowFlags(ANativeActivity *, int32_t, int32_t);

/* log */
enum { NOTA_LOG_INFO = 4, NOTA_LOG_ERROR = 6 };
int __android_log_print(int prio, const char *tag, const char *fmt, ...);

/* The one symbol the app glue looks up in the library. Implemented in
 * src/main.S; declared here so the glue can call it. */
void ANativeActivity_onCreate(ANativeActivity *activity, void *savedState,
                              size_t savedStateSize);

/* Implemented in src/callbacks.S: dispatch one input event. */
int nota_app_input(struct android_app *app, AInputEvent *event);

/* Called from src/lifecycle.S when the queue appears; starts the pump thread
 * once and returns 1 if it is running. */
int nota_glue_start_input(struct android_app *app);

#endif /* NOTA_GLUE_H */
