#include "csi_common.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_wifi.h"

static const char *TAG = "CSI_CAP";

static csi_frame_t s_frame_pool[CSI_QUEUE_DEPTH];
static QueueHandle_t s_free_q;
static QueueHandle_t s_ready_q;
static uint32_t s_drop_count;

void csi_queues_init(void)
{
    s_free_q = xQueueCreate(CSI_QUEUE_DEPTH, sizeof(uint8_t));
    s_ready_q = xQueueCreate(CSI_QUEUE_DEPTH, sizeof(uint8_t));
    if (s_free_q == NULL || s_ready_q == NULL) {
        ESP_LOGE(TAG, "Failed to create CSI queues");
        abort();
    }
    for (uint8_t i = 0; i < CSI_QUEUE_DEPTH; i++) {
        xQueueSend(s_free_q, &i, 0);
    }
}

uint32_t csi_drop_count(void)
{
    return s_drop_count;
}

QueueHandle_t csi_ready_queue(void)
{
    return s_ready_q;
}

csi_frame_t *csi_frame_at(uint8_t idx)
{
    if (idx >= CSI_QUEUE_DEPTH) {
        return NULL;
    }
    return &s_frame_pool[idx];
}

void csi_release_frame(uint8_t idx)
{
    xQueueSend(s_free_q, &idx, portMAX_DELAY);
}

void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
{
    (void)ctx;

    if (info == NULL || info->buf == NULL || info->len == 0) {
        return;
    }
    if (s_free_q == NULL || s_ready_q == NULL) {
        return;
    }

    uint8_t idx = 0;
    if (xQueueReceive(s_free_q, &idx, 0) != pdTRUE) {
        s_drop_count++;
        return;
    }

    static uint32_t s_seq = 0;
    csi_frame_t *frame = &s_frame_pool[idx];

    frame->seq = ++s_seq;
    frame->rssi = info->rx_ctrl.rssi;
    frame->noise_floor = info->rx_ctrl.noise_floor;
    frame->channel = info->rx_ctrl.channel;
    frame->secondary_channel = info->rx_ctrl.secondary_channel;
    frame->sig_mode = info->rx_ctrl.sig_mode;
    frame->mcs = info->rx_ctrl.mcs;
    frame->cwb = info->rx_ctrl.cwb;
    frame->stbc = info->rx_ctrl.stbc;
    memcpy(frame->mac, info->mac, sizeof(frame->mac));
    frame->first_word_invalid = info->first_word_invalid ? 1 : 0;

    const uint16_t copy_len = (info->len > CSI_MAX_LEN) ? CSI_MAX_LEN : (uint16_t)info->len;
    frame->len = copy_len;
    memcpy(frame->buf, info->buf, copy_len);

    if (xQueueSend(s_ready_q, &idx, 0) != pdTRUE) {
        xQueueSend(s_free_q, &idx, 0);
        s_drop_count++;
    }
}

void wifi_csi_engine_start(void)
{
    wifi_csi_config_t csi_config = {};
    csi_config.lltf_en = true;
    csi_config.htltf_en = true;
    csi_config.stbc_htltf2_en = true;
    csi_config.ltf_merge_en = true;
    csi_config.channel_filter_en = false;
    csi_config.manu_scale = false;
    csi_config.shift = 0;
    csi_config.dump_ack_en = false;

    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));
    ESP_LOGI(TAG, "CSI engine enabled");
}

int csi_format_csv(char *line, size_t line_cap, uint8_t node_id, const csi_frame_t *frame)
{
    if (line == NULL || frame == NULL || line_cap < 64) {
        return -1;
    }

    int pos = snprintf(
        line, line_cap,
        "CSI,%u,%lu,%d,%d,%u,%u,%u,%u,%u,%u,%02x%02x%02x%02x%02x%02x,%u,%u",
        (unsigned)node_id,
        (unsigned long)frame->seq,
        (int)frame->rssi,
        (int)frame->noise_floor,
        (unsigned)frame->channel,
        (unsigned)frame->secondary_channel,
        (unsigned)frame->sig_mode,
        (unsigned)frame->mcs,
        (unsigned)frame->cwb,
        (unsigned)frame->stbc,
        frame->mac[0], frame->mac[1], frame->mac[2],
        frame->mac[3], frame->mac[4], frame->mac[5],
        (unsigned)frame->len,
        (unsigned)frame->first_word_invalid);

    if (pos < 0 || pos >= (int)line_cap) {
        return -1;
    }

    for (uint16_t i = 0; i < frame->len; i++) {
        const int n = snprintf(line + pos, line_cap - (size_t)pos, ",%d", (int)frame->buf[i]);
        if (n < 0 || n >= (int)(line_cap - (size_t)pos)) {
            break;
        }
        pos += n;
    }
    return pos;
}

int csi_frame_to_udp(uint8_t *out, size_t out_cap, uint8_t node_id, const csi_frame_t *frame)
{
    if (out == NULL || frame == NULL) {
        return -1;
    }
    const size_t need = sizeof(csi_udp_hdr_t) + frame->len;
    if (need > out_cap || frame->len > CSI_MAX_LEN) {
        return -1;
    }

    csi_udp_hdr_t *hdr = (csi_udp_hdr_t *)out;
    hdr->magic = CSI_UDP_MAGIC;
    hdr->version = CSI_UDP_VERSION;
    hdr->node_id = node_id;
    hdr->seq = frame->seq;
    hdr->rssi = frame->rssi;
    hdr->noise = frame->noise_floor;
    hdr->channel = frame->channel;
    hdr->secondary_channel = frame->secondary_channel;
    hdr->sig_mode = frame->sig_mode;
    hdr->mcs = frame->mcs;
    hdr->cwb = frame->cwb;
    hdr->stbc = frame->stbc;
    memcpy(hdr->mac, frame->mac, 6);
    hdr->len = frame->len;
    hdr->fw_inv = frame->first_word_invalid;
    memcpy(out + sizeof(csi_udp_hdr_t), frame->buf, frame->len);
    return (int)need;
}

bool csi_udp_to_frame(const uint8_t *data, size_t len, csi_frame_t *frame, uint8_t *node_id_out)
{
    if (data == NULL || frame == NULL || node_id_out == NULL) {
        return false;
    }
    if (len < sizeof(csi_udp_hdr_t)) {
        return false;
    }

    const csi_udp_hdr_t *hdr = (const csi_udp_hdr_t *)data;
    if (hdr->magic != CSI_UDP_MAGIC || hdr->version != CSI_UDP_VERSION) {
        return false;
    }
    if (hdr->len > CSI_MAX_LEN) {
        return false;
    }
    if (len < sizeof(csi_udp_hdr_t) + hdr->len) {
        return false;
    }

    *node_id_out = hdr->node_id;
    frame->seq = hdr->seq;
    frame->rssi = hdr->rssi;
    frame->noise_floor = hdr->noise;
    frame->channel = hdr->channel;
    frame->secondary_channel = hdr->secondary_channel;
    frame->sig_mode = hdr->sig_mode;
    frame->mcs = hdr->mcs;
    frame->cwb = hdr->cwb;
    frame->stbc = hdr->stbc;
    memcpy(frame->mac, hdr->mac, 6);
    frame->len = hdr->len;
    frame->first_word_invalid = hdr->fw_inv ? 1 : 0;
    memcpy(frame->buf, data + sizeof(csi_udp_hdr_t), hdr->len);
    return true;
}
