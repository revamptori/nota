/* glue.c - the input pump thread.
 *
 * This is the only C in NOTA. It exists because an AInputQueue must be read
 * from a looper, and a looper that nobody polls does not deliver anything;
 * the looper has to live on a thread that is not the one running the
 * callbacks. Everything it dispatches to is assembly:
 *
 *     nota_app_input(app, event)   -> 1 if the event was consumed
 *
 * Window, lifecycle and EGL work all happen on the Java thread inside the
 * ANativeActivityCallbacks that src/main.S installs, so no other C is needed.
 */
#include "glue.h"

/* pthreads, declared rather than included: the build has no NDK sysroot, and
 * the only part of the ABI used here is "make a thread and never join it".
 * pthread_t is 8 bytes on LP64 (a pointer to the thread control block). */
typedef unsigned long nota_pthread_t;
typedef struct { void *opaque[7]; } nota_pthread_attr_t;
int pthread_create(nota_pthread_t *thread, const nota_pthread_attr_t *attr,
                   void *(*start)(void *), void *arg);

static nota_pthread_t g_thread;
static int g_started;

static void *input_thread(void *param) {
    struct android_app *app = (struct android_app *)param;
    ALooper *looper = app->looper ? app->looper : ALooper_prepare(0);
    if (!looper) {
        return NULL;
    }
    app->looper = looper;

    AInputQueue *attached = NULL;
    while (!app->destroyed) {
        /* Pick up a queue that onInputQueueCreated published. */
        AInputQueue *queue = app->inputQueue;
        if (queue != attached) {
            if (attached) {
                AInputQueue_detachLooper(attached);
            }
            attached = queue;
            if (attached) {
                AInputQueue_attachLooper(attached, looper, NOTA_LOOPER_ID_INPUT,
                                         NULL, app);
            }
        }

        void *source = NULL;
        int events = 0;
        int ident = ALooper_pollOnce(250 /* ms */, NULL, &events, &source);
        if (ident != NOTA_LOOPER_ID_INPUT || !attached) {
            continue;
        }

        AInputEvent *event = NULL;
        while (AInputQueue_getEvent(attached, &event) >= 0) {
            if (AInputQueue_preDispatchEvent(attached, event)) {
                continue;
            }
            int handled = nota_app_input(app, event);
            AInputQueue_finishEvent(attached, event, handled);
        }
    }

    if (attached) {
        AInputQueue_detachLooper(attached);
    }
    return NULL;
}

/* Called from src/main.S once, on the Java thread, when the queue appears. */
int nota_glue_start_input(struct android_app *app) {
    if (g_started) {
        return 0;
    }
    g_started = 1;
    return pthread_create(&g_thread, 0, input_thread, app) == 0 ? 1 : 0;
}
