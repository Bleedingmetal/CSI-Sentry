#include <stdio.h>
#include <string.h>

#include "nvs_flash.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "rom/ets_sys.h"

static const char *TAG = "CSI_SENTRY";

#ifndef CSI_NODE_ID
#define CSI_NODE_ID 1
#endif

#ifndef CSI_LISTEN_CHANNEL
#define CSI_LISTEN_CHANNEL 1
#endif

#ifndef CSI_MAX_LEN
#define CSI_MAX_LEN 612
#endif

#ifndef CSI_QUEUE_DEPTH
#define CSI_QUEUE_DEPTH 8
#endif

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

static csi_frame_t s_frame_pool[CSI_QUEUE_DEPTH];
static QueueHandle_t s_free_q;
static QueueHandle_t s_ready_q;
static uint32_t s_drop_count;

static void wifi_csi_rx_cb(void *ctx, wifi_csi_info_t *info)
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

    const uint16_t copy_len = (info->len > CSI_MAX_LEN) ? CSI_MAX_LEN : info->len;
    frame->len = copy_len;
    memcpy(frame->buf, info->buf, copy_len);

    if (xQueueSend(s_ready_q, &idx, 0) != pdTRUE) {
        xQueueSend(s_free_q, &idx, 0);
        s_drop_count++;
    }
}

static void csi_serial_task(void *arg)
{
    (void)arg;
    uint8_t idx = 0;
    static char line[4096];

    ets_printf("# CSI_FMT,1,node,seq,rssi,noise,ch,sec_ch,sig_mode,mcs,bw,stbc,mac,len,fw_inv,csi...\n");
    ets_printf("# CSI_NODE,%u\n", (unsigned)CSI_NODE_ID);

    while (true) {
        if (xQueueReceive(s_ready_q, &idx, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        const csi_frame_t *frame = &s_frame_pool[idx];

        int pos = snprintf(
            line, sizeof(line),
            "CSI,%u,%lu,%d,%d,%u,%u,%u,%u,%u,%u,%02x%02x%02x%02x%02x%02x,%u,%u",
            (unsigned)CSI_NODE_ID,
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

        if (pos > 0 && pos < (int)sizeof(line)) {
            for (uint16_t i = 0; i < frame->len; i++) {
                const int n = snprintf(line + pos, sizeof(line) - (size_t)pos,
                                       ",%d", (int)frame->buf[i]);
                if (n < 0 || n >= (int)(sizeof(line) - (size_t)pos)) {
                    break;
                }
                pos += n;
            }
            ets_printf("%s\n", line);
        }

        xQueueSend(s_free_q, &idx, portMAX_DELAY);
    }
}

static void wifi_init_sta_radio(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_LOGI(TAG, "wifi_init: csi_enable=%d node_id=%u", cfg.csi_enable, (unsigned)CSI_NODE_ID);
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW20));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
}

static void wifi_enable_promiscuous_on_channel(uint8_t primary_channel)
{
    wifi_promiscuous_filter_t filter = {
        .filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT | WIFI_PROMIS_FILTER_MASK_DATA,
    };

    ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filter));
    ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
    ESP_ERROR_CHECK(esp_wifi_set_channel(primary_channel, WIFI_SECOND_CHAN_NONE));
    vTaskDelay(pdMS_TO_TICKS(100));

    bool promisc = false;
    ESP_ERROR_CHECK(esp_wifi_get_promiscuous(&promisc));
    if (!promisc) {
        ESP_LOGE(TAG, "Promiscuous mode failed to enable");
        abort();
    }

    uint8_t ch = 0;
    wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
    ESP_ERROR_CHECK(esp_wifi_get_channel(&ch, &second));
    ESP_LOGI(TAG, "Radio ready: promiscuous=%d primary_channel=%u", (int)promisc, (unsigned)ch);
}

static void wifi_csi_engine_start(void)
{
    wifi_csi_config_t csi_config = {};
    csi_config.lltf_en           = true;
    csi_config.htltf_en          = true;
    csi_config.stbc_htltf2_en    = true;
    csi_config.ltf_merge_en      = true;
    csi_config.channel_filter_en = false;
    csi_config.manu_scale        = false;
    csi_config.shift             = 0;
    csi_config.dump_ack_en       = false;

    ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(wifi_csi_rx_cb, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi_config));
    ESP_ERROR_CHECK(esp_wifi_set_csi(true));

    ESP_LOGI(TAG, "CSI engine enabled (non-HE classic ESP32 config)");
}

static void csi_queues_init(void)
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

extern "C" void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    csi_queues_init();

    BaseType_t ok = xTaskCreatePinnedToCore(
        csi_serial_task, "csi_serial", 4096, NULL, 5, NULL, 0);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "Failed to create CSI serial task");
        abort();
    }

    wifi_init_sta_radio();
    wifi_enable_promiscuous_on_channel(CSI_LISTEN_CHANNEL);
    wifi_csi_engine_start();

    ESP_LOGI(TAG, "CSI-Sentry node %u streaming on channel %d @ 921600 baud",
             (unsigned)CSI_NODE_ID, CSI_LISTEN_CHANNEL);

    while (true) {
        ESP_LOGI(TAG, "queue_drops=%lu", (unsigned long)s_drop_count);
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}
