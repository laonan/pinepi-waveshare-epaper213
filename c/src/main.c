#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <pthread.h>
#include <time.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/un.h>

#include "DEV_Config.h"
#include "EPD_2in13_V3.h"
#include "GT1151.h"
#include "GUI_Paint.h"

#define PINEPI_DISPLAY_BUILD "full-refresh-on-change-2026-05-30"
#define DISPLAY_IMAGE_SIZE 4000
#define FULL_REFRESH_EVERY 10
#define FULL_REFRESH_DIFF_BYTES 1

/* -------------------------------------------------------------------------- */
/*  Hardware & Display Globals                                               */
/* -------------------------------------------------------------------------- */
extern GT1151_Dev Dev_Now;
extern int IIC_Address;

static volatile int g_running = 1;
static volatile int g_pending_refresh = 0;

static unsigned char g_recv_buffer[DISPLAY_IMAGE_SIZE];
static UBYTE *g_black_image = NULL;
static UBYTE *g_white_image = NULL;

/* Touch debounce state */
static int s_touch_active = 0;
static int s_no_touch_count = 0;

/* Refresh rate limiting (protect e-paper from overly frequent refresh) */
static time_t g_last_refresh_time = 0;
#define MIN_REFRESH_INTERVAL_SEC 1

/* Partial/full refresh cycle counter */
static int s_refresh_count = 0;
static int s_partial_since_full = 0;

/* Unix Domain Socket for sending TAP events */
static int s_tap_sock = -1;
static struct sockaddr_un s_tap_addr;
#define TAP_SOCKET_PATH "/tmp/pinepi-touch.sock"

/* Unix Domain Socket for receiving images */
static int g_uds_sock = -1;
#define DISPLAY_SOCKET_PATH "/tmp/pinepi.sock"

static void clear_touch_points(void)
{
    Dev_Now.TouchpointFlag = 0;
    Dev_Now.TouchCount = 0;
    for (int i = 0; i < CT_MAX_TOUCH; i++) {
        Dev_Now.X[i] = 0;
        Dev_Now.Y[i] = 0;
        Dev_Now.S[i] = 0;
        Dev_Now.Touchkeytrackid[i] = 0;
    }
}

static int count_changed_bytes(const UBYTE *old_image, const UBYTE *new_image, size_t len)
{
    int changed = 0;
    for (size_t i = 0; i < len; i++) {
        if (old_image[i] != new_image[i]) {
            changed++;
        }
    }
    return changed;
}

/* -------------------------------------------------------------------------- */
/*  Signal Handler                                                            */
/* -------------------------------------------------------------------------- */
static void signal_handler(int signo)
{
    (void)signo;
    printf("[Display] Caught signal, shutting down...\n");
    g_running = 0;
    /* Force recv() to return immediately so the UDS thread can exit */
    if (g_uds_sock >= 0) {
        close(g_uds_sock);
        g_uds_sock = -1;
    }
}

/* -------------------------------------------------------------------------- */
/*  Unix Domain Socket Receiver Thread                                       */
/*  Receives exactly 4000 bytes and asks main loop to refresh.               */
/* -------------------------------------------------------------------------- */
static void *uds_receiver_thread(void *arg)
{
    (void)arg;

    /* Remove old socket file if exists */
    unlink(DISPLAY_SOCKET_PATH);

    g_uds_sock = socket(AF_UNIX, SOCK_STREAM, 0);
    if (g_uds_sock < 0) {
        perror("[Display] socket(UDS)");
        pthread_exit(NULL);
    }

    int reuse = 1;
    setsockopt(g_uds_sock, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    /* 1-second receive timeout so we can check g_running periodically */
    struct timeval tv;
    tv.tv_sec = 1;
    tv.tv_usec = 0;
    setsockopt(g_uds_sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, DISPLAY_SOCKET_PATH, sizeof(addr.sun_path) - 1);

    if (bind(g_uds_sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("[Display] bind(UDS)");
        close(g_uds_sock);
        g_uds_sock = -1;
        pthread_exit(NULL);
    }

    if (listen(g_uds_sock, 1) < 0) {
        perror("[Display] listen(UDS)");
        close(g_uds_sock);
        g_uds_sock = -1;
        pthread_exit(NULL);
    }

    printf("[Display] UDS receiver listening on %s\n", DISPLAY_SOCKET_PATH);

    while (g_running) {
        int client_sock = accept(g_uds_sock, NULL, NULL);
        if (client_sock < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                continue;
            }
            if (g_running) perror("[Display] accept");
            continue;
        }

        /* Read exactly DISPLAY_IMAGE_SIZE bytes */
        ssize_t total = 0;
        while (total < DISPLAY_IMAGE_SIZE && g_running) {
            ssize_t n = recv(client_sock, g_recv_buffer + total, DISPLAY_IMAGE_SIZE - total, 0);
            if (n <= 0) {
                break;
            }
            total += n;
        }
        close(client_sock);

        if (total != DISPLAY_IMAGE_SIZE) {
            printf("[Display] WARN: received %zd bytes, expected %d\n", total, DISPLAY_IMAGE_SIZE);
            continue;
        }

        /* Rate limiting: protect e-paper from overly frequent refresh */
        time_t now = time(NULL);
        if (g_last_refresh_time > 0 && (now - g_last_refresh_time) < MIN_REFRESH_INTERVAL_SEC) {
            int wait_ms = (MIN_REFRESH_INTERVAL_SEC - (int)(now - g_last_refresh_time)) * 1000;
            printf("[Display] Refresh too frequent; waiting %dms instead of dropping frame.\n", wait_ms);
            DEV_Delay_ms(wait_ms);
        }

        printf("[Display] Received %zd bytes, requesting refresh...\n", total);
        g_pending_refresh = 1;

        /* Wait for main loop to finish refresh before listening again */
        while (g_pending_refresh && g_running) {
            DEV_Delay_ms(10);
        }
    }

    if (g_uds_sock >= 0) {
        close(g_uds_sock);
        g_uds_sock = -1;
    }
    unlink(DISPLAY_SOCKET_PATH);
    printf("[Display] UDS receiver stopped.\n");
    pthread_exit(NULL);
}

/* -------------------------------------------------------------------------- */
/*  Touch → TAP emitter (JSON format with timestamp)                         */
/* -------------------------------------------------------------------------- */
static void emit_tap_event(void)
{
    if (s_tap_sock < 0) return;

    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    long long ms = (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;

    char msg[128];
    snprintf(msg, sizeof(msg), "{\"type\":\"tap\",\"ts\":%lld}", ms);

    sendto(s_tap_sock, msg, strlen(msg), 0,
           (struct sockaddr *)&s_tap_addr, sizeof(s_tap_addr));
    printf("[Display] TAP event sent: %s\n", msg);
}

/* -------------------------------------------------------------------------- */
/*  Screen Refresh (called from main loop only)                               */
/* -------------------------------------------------------------------------- */
static void refresh_screen(void)
{
    int changed = count_changed_bytes(g_black_image, g_recv_buffer, DISPLAY_IMAGE_SIZE);
    int use_full_refresh = (s_refresh_count == 0) ||
                           (s_partial_since_full >= FULL_REFRESH_EVERY) ||
                           (changed >= FULL_REFRESH_DIFF_BYTES);

    printf("[Display] Refreshing screen (count=%d partials=%d changed=%d mode=%s)...\n",
           s_refresh_count,
           s_partial_since_full,
           changed,
           use_full_refresh ? "FULL" : "PART");
    g_last_refresh_time = time(NULL);

    if (use_full_refresh) {
        /* ---- Full refresh: first frame, large changes, or periodic cleanup ---- */
        EPD_2in13_V3_Init(EPD_2IN13_V3_FULL);
        DEV_Delay_ms(100);
        memcpy(g_black_image, g_recv_buffer, DISPLAY_IMAGE_SIZE);
        EPD_2in13_V3_Display(g_black_image);
        DEV_Delay_ms(2500);
        EPD_2in13_V3_Sleep();
        printf("[Display] Full refresh complete. Screen put to sleep.\n");
        s_partial_since_full = 0;
    } else {
        /* ---- Partial refresh: use only for small same-page changes ---- */
        EPD_2in13_V3_Init(EPD_2IN13_V3_PART);
        DEV_Delay_ms(100);
        /* Load old frame as base, then push delta */
        EPD_2in13_V3_Display_Base(g_black_image);
        EPD_2in13_V3_Display_Partial_Wait(g_recv_buffer);
        DEV_Delay_ms(300);
        /* Update cached base frame for next partial refresh */
        memcpy(g_black_image, g_recv_buffer, DISPLAY_IMAGE_SIZE);
        EPD_2in13_V3_Sleep();
        printf("[Display] Partial refresh complete. Screen put to sleep.\n");
        s_partial_since_full++;
    }

    /* Wait for touch release after refresh (up to 3s timeout) */
    printf("[Display] Waiting for touch release...\n");
    int release_wait_ms = 0;
    while (release_wait_ms < 3000) {
        Dev_Now.Touch = 1;
        GT_Scan();
        if (Dev_Now.X[0] == 0 && Dev_Now.Y[0] == 0) {
            break;  /* Touch released */
        }
        DEV_Delay_ms(50);
        release_wait_ms += 50;
    }
    if (release_wait_ms >= 3000) {
        printf("[Display] Touch release timeout; forcing reset.\n");
    }

    clear_touch_points();
    s_touch_active = 0;
    s_no_touch_count = 5;

    s_refresh_count++;
}

/* -------------------------------------------------------------------------- */
/*  Main                                                                      */
/* -------------------------------------------------------------------------- */
int main(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    setvbuf(stderr, NULL, _IOLBF, 0);
    printf("[Display] build=%s\n", PINEPI_DISPLAY_BUILD);

    IIC_Address = 0x14;
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = signal_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0; /* No SA_RESTART: interrupt blocked syscalls */
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);

    /* ---- Start UDS receiver thread FIRST to avoid race with Python ---- */
    pthread_t uds_thread;
    pthread_create(&uds_thread, NULL, uds_receiver_thread, NULL);

    /* ---- UDS TAP sender init --------------------------------------------- */
    s_tap_sock = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (s_tap_sock >= 0) {
        memset(&s_tap_addr, 0, sizeof(s_tap_addr));
        s_tap_addr.sun_family = AF_UNIX;
        strncpy(s_tap_addr.sun_path, TAP_SOCKET_PATH, sizeof(s_tap_addr.sun_path) - 1);
    } else {
        perror("[Display] TAP socket");
    }

    /* ---- Hardware init --------------------------------------------------- */
    DEV_ModuleInit();
    GT_Init();
    DEV_Delay_ms(100);

    UWORD image_size = ((EPD_2in13_V3_WIDTH % 8 == 0)
                        ? (EPD_2in13_V3_WIDTH / 8)
                        : (EPD_2in13_V3_WIDTH / 8 + 1))
                       * EPD_2in13_V3_HEIGHT;   /* = 4000 bytes */

    g_black_image = (UBYTE *)malloc(image_size);
    g_white_image = (UBYTE *)malloc(image_size);
    if (g_black_image == NULL || g_white_image == NULL) {
        fprintf(stderr, "[Display] Failed to allocate image buffer\n");
        return -1;
    }
    memset(g_white_image, 0xFF, image_size);  /* WHITE = 0xFF */

    Paint_NewImage(g_black_image, EPD_2in13_V3_WIDTH,
                   EPD_2in13_V3_HEIGHT, 270, WHITE);
    Paint_SelectImage(g_black_image);
    Paint_SetMirroring(MIRROR_ORIGIN);
    Paint_Clear(WHITE);

    /* Initial clear */
    EPD_2in13_V3_Init(EPD_2IN13_V3_FULL);
    EPD_2in13_V3_Display(g_black_image);
    DEV_Delay_ms(2500);
    EPD_2in13_V3_Sleep();  /* sleep after initial clear */

    printf("[Display] pinepi-waveshare-epaper213 started. Waiting for images on UDS %s...\n", DISPLAY_SOCKET_PATH);

    /* ---- Main loop ------------------------------------------------------- */
    while (g_running) {
        /* Handle pending refresh (only here, so GT_Scan is never contested) */
        if (g_pending_refresh) {
            refresh_screen();
            g_pending_refresh = 0;
            continue;
        }

        /* Poll GT1151 in forced mode so GT_Scan() always reads I2C */
        Dev_Now.Touch = 1;
        GT_Scan();

        if (Dev_Now.X[0] > 0 && Dev_Now.Y[0] > 0) {
            s_no_touch_count = 0;

            if (!s_touch_active) {
                emit_tap_event();
                s_touch_active = 1;
                DEV_Delay_ms(300); /* Anti-bounce: 300ms cooldown after tap */

                /* Consume the coordinate so we don't re-trigger immediately */
                clear_touch_points();
            }
        } else {
            s_no_touch_count++;
            if (s_no_touch_count > 3) {
                s_touch_active = 0;
            }
        }

        DEV_Delay_ms(50);  /* 50ms scan interval as per architecture doc */
    }

    /* ---- Shutdown -------------------------------------------------------- */
    printf("[Display] Shutting down...\n");
    pthread_join(uds_thread, NULL);

    if (s_tap_sock >= 0) close(s_tap_sock);

    /* Refresh white before sleep to avoid long-term fixed image damage */
    printf("[Display] Clearing to white before exit...\n");
    EPD_2in13_V3_Init(EPD_2IN13_V3_FULL);
    EPD_2in13_V3_Display(g_white_image);
    DEV_Delay_ms(2000);
    EPD_2in13_V3_Sleep();
    DEV_Delay_ms(1000);
    DEV_ModuleExit();

    free(g_black_image);
    free(g_white_image);

    printf("[Display] Exited cleanly.\n");
    return 0;
}
