#include "csi_common.h"

#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "CSI_SENTRY";

#if CSI_ROLE == CSI_ROLE_LEGACY
static void wifi_start_legacy_sniffer(void)
{
    esp_netif_create_default_wifi_sta();
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW20));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    wifi_enable_promiscuous_on_channel(CSI_LISTEN_CHANNEL);
}
#endif

extern "C" void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    csi_queues_init();
    wifi_init_common();

#if CSI_ROLE == CSI_ROLE_AGG
    ESP_LOGI(TAG, "Role=AGGREGATOR node=%u (USB → PC)", (unsigned)CSI_NODE_ID);
    wifi_start_softap_aggregator();
    wifi_csi_engine_start();
    agg_start_tasks();
#elif CSI_ROLE == CSI_ROLE_REMOTE
    ESP_LOGI(TAG, "Role=REMOTE node=%u (power only → Wi-Fi)", (unsigned)CSI_NODE_ID);
    wifi_start_sta_remote();
    wifi_csi_engine_start();
    remote_start_tasks();
#else
    ESP_LOGI(TAG, "Role=LEGACY node=%u sniff ch=%u", (unsigned)CSI_NODE_ID,
             (unsigned)CSI_LISTEN_CHANNEL);
    wifi_start_legacy_sniffer();
    wifi_csi_engine_start();
    legacy_start_serial_task();
#endif

    while (true) {
        ESP_LOGI(TAG, "alive role=%d node=%u queue_drops=%lu",
                 CSI_ROLE, (unsigned)CSI_NODE_ID, (unsigned long)csi_drop_count());
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}
