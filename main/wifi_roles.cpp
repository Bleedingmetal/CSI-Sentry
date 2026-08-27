#include "csi_common.h"

#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"

static const char *TAG = "CSI_WIFI";

static EventGroupHandle_t s_wifi_events;
static const int WIFI_GOT_IP_BIT = BIT0;

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)data;

    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "STA disconnected — reconnecting");
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "STA got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        if (s_wifi_events) {
            xEventGroupSetBits(s_wifi_events, WIFI_GOT_IP_BIT);
        }
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STACONNECTED) {
        wifi_event_ap_staconnected_t *e = (wifi_event_ap_staconnected_t *)data;
        ESP_LOGI(TAG, "Remote joined SoftAP: %02x:%02x:%02x:%02x:%02x:%02x",
                 e->mac[0], e->mac[1], e->mac[2], e->mac[3], e->mac[4], e->mac[5]);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STADISCONNECTED) {
        wifi_event_ap_stadisconnected_t *e = (wifi_event_ap_stadisconnected_t *)data;
        ESP_LOGW(TAG, "Remote left SoftAP: %02x:%02x:%02x:%02x:%02x:%02x",
                 e->mac[0], e->mac[1], e->mac[2], e->mac[3], e->mac[4], e->mac[5]);
    }
}

void wifi_init_common(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    s_wifi_events = xEventGroupCreate();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_LOGI(TAG, "wifi_init: csi_enable=%d node=%u role=%d",
             cfg.csi_enable, (unsigned)CSI_NODE_ID, CSI_ROLE);
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL));
}

void wifi_enable_promiscuous_on_channel(uint8_t primary_channel)
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
        ESP_LOGE(TAG, "Promiscuous mode failed");
        abort();
    }
    ESP_LOGI(TAG, "Promiscuous on channel %u", (unsigned)primary_channel);
}

void wifi_start_softap_aggregator(void)
{
    esp_netif_create_default_wifi_ap();

    wifi_config_t ap = {};
    strncpy((char *)ap.ap.ssid, CSI_SOFTAP_SSID, sizeof(ap.ap.ssid) - 1);
    strncpy((char *)ap.ap.password, CSI_SOFTAP_PASS, sizeof(ap.ap.password) - 1);
    ap.ap.ssid_len = strlen(CSI_SOFTAP_SSID);
    ap.ap.channel = CSI_LISTEN_CHANNEL;
    ap.ap.max_connection = 4;
    ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
    ap.ap.beacon_interval = 100;
    if (strlen(CSI_SOFTAP_PASS) == 0) {
        ap.ap.authmode = WIFI_AUTH_OPEN;
    }

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_AP, WIFI_BW20));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    ESP_LOGI(TAG, "SoftAP '%s' ch=%u  remotes join → UDP %u  (agg IP 192.168.4.1)",
             CSI_SOFTAP_SSID, (unsigned)CSI_LISTEN_CHANNEL, (unsigned)CSI_UDP_PORT);
}

void wifi_start_sta_remote(void)
{
    esp_netif_create_default_wifi_sta();

    wifi_config_t sta = {};
    strncpy((char *)sta.sta.ssid, CSI_SOFTAP_SSID, sizeof(sta.sta.ssid) - 1);
    strncpy((char *)sta.sta.password, CSI_SOFTAP_PASS, sizeof(sta.sta.password) - 1);
    sta.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    sta.sta.pmf_cfg.capable = true;
    sta.sta.pmf_cfg.required = false;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW20));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    ESP_LOGI(TAG, "STA connecting to SoftAP '%s'...", CSI_SOFTAP_SSID);
    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_events, WIFI_GOT_IP_BIT, pdFALSE, pdTRUE, pdMS_TO_TICKS(30000));
    if ((bits & WIFI_GOT_IP_BIT) == 0) {
        ESP_LOGE(TAG, "Failed to get IP from aggregator SoftAP");
        abort();
    }
}
