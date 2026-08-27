#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#ifndef CSI_NODE_ID
#define CSI_NODE_ID 1
#endif

#ifndef CSI_LISTEN_CHANNEL
#define CSI_LISTEN_CHANNEL 6
#endif

#ifndef CSI_MAX_LEN
#define CSI_MAX_LEN 612
#endif

#ifndef CSI_QUEUE_DEPTH
#define CSI_QUEUE_DEPTH 8
#endif

#ifndef CSI_UDP_PORT
#define CSI_UDP_PORT 5005
#endif

#ifndef CSI_TCP_PORT
#define CSI_TCP_PORT 5006
#endif

#ifndef CSI_ROLE
#define CSI_ROLE 2
#endif

#define CSI_ROLE_LEGACY 0
#define CSI_ROLE_REMOTE 1
#define CSI_ROLE_AGG    2

#ifndef CSI_SOFTAP_SSID
#define CSI_SOFTAP_SSID "CSI-Sentry"
#endif

#ifndef CSI_SOFTAP_PASS
#define CSI_SOFTAP_PASS "csisentry"
#endif

#ifndef CSI_REMOTE_HZ
#define CSI_REMOTE_HZ 20
#endif

#define CSI_UDP_MAGIC   0xC511u
#define CSI_UDP_VERSION 1u
#define CSI_LINE_MAX    5120

#if !CONFIG_ESP_WIFI_CSI_ENABLED
#error "Enable Wi-Fi CSI in menuconfig: Component config -> Wi-Fi -> Wi-Fi CSI"
#endif

typedef struct {
    uint32_t seq;
    int8_t   rssi;
    int8_t   noise_floor;
    uint8_t  channel;
    uint8_t  secondary_channel;
    uint8_t  sig_mode;
    uint8_t  mcs;
    uint8_t  cwb;
    uint8_t  stbc;
    uint8_t  mac[6];
    uint16_t len;
    uint8_t  first_word_invalid;
    int8_t   buf[CSI_MAX_LEN];
} csi_frame_t;

#pragma pack(push, 1)
typedef struct {
    uint16_t magic;
    uint8_t  version;
    uint8_t  node_id;
    uint32_t seq;
    int8_t   rssi;
    int8_t   noise;
    uint8_t  channel;
    uint8_t  secondary_channel;
    uint8_t  sig_mode;
    uint8_t  mcs;
    uint8_t  cwb;
    uint8_t  stbc;
    uint8_t  mac[6];
    uint16_t len;
    uint8_t  fw_inv;
} csi_udp_hdr_t;
#pragma pack(pop)

#define CSI_UDP_MAX (sizeof(csi_udp_hdr_t) + CSI_MAX_LEN)

#ifdef __cplusplus
extern "C" {
#endif

void csi_queues_init(void);
uint32_t csi_drop_count(void);
QueueHandle_t csi_ready_queue(void);
csi_frame_t *csi_frame_at(uint8_t idx);
void csi_release_frame(uint8_t idx);

void wifi_csi_engine_start(void);
void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info);

int csi_format_csv(char *line, size_t line_cap, uint8_t node_id, const csi_frame_t *frame);
int csi_frame_to_udp(uint8_t *out, size_t out_cap, uint8_t node_id, const csi_frame_t *frame);
bool csi_udp_to_frame(const uint8_t *data, size_t len, csi_frame_t *frame, uint8_t *node_id_out);

void wifi_init_common(void);
void wifi_start_softap_aggregator(void);
void wifi_start_sta_remote(void);
void wifi_enable_promiscuous_on_channel(uint8_t primary_channel);

void agg_start_tasks(void);
void remote_start_tasks(void);
void legacy_start_serial_task(void);

#ifdef __cplusplus
}
#endif
