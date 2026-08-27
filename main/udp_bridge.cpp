#include "csi_common.h"

#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "rom/ets_sys.h"
#include <unistd.h>

static const char *TAG = "CSI_UDP";

static SemaphoreHandle_t s_client_mu;
static int s_tcp_client = -1;

static void print_banner(const char *role)
{
    ets_printf("# CSI_FMT,1,node,seq,rssi,noise,ch,sec_ch,sig_mode,mcs,bw,stbc,mac,len,fw_inv,csi...\n");
    ets_printf("# CSI_NODE,%u\n", (unsigned)CSI_NODE_ID);
    ets_printf("# CSI_ROLE,%s\n", role);
    ets_printf("# CSI_NOTE,join SoftAP then: python host/csi_presence.py --tcp 192.168.4.1:%u\n",
               (unsigned)CSI_TCP_PORT);
}

static void tcp_set_client(int fd)
{
    if (s_client_mu == NULL) {
        return;
    }
    xSemaphoreTake(s_client_mu, portMAX_DELAY);
    if (s_tcp_client >= 0 && s_tcp_client != fd) {
        shutdown(s_tcp_client, 0);
        close(s_tcp_client);
    }
    s_tcp_client = fd;
    xSemaphoreGive(s_client_mu);
}

static void emit_line(const char *line)
{
    ets_printf("%s\n", line);

    if (s_client_mu == NULL) {
        return;
    }
    xSemaphoreTake(s_client_mu, portMAX_DELAY);
    const int fd = s_tcp_client;
    if (fd >= 0) {
        char out[CSI_LINE_MAX + 2];
        const int n = snprintf(out, sizeof(out), "%s\n", line);
        if (n > 0) {
            const int w = send(fd, out, (size_t)n, 0);
            if (w < 0) {
                ESP_LOGW(TAG, "TCP client disconnected");
                close(fd);
                s_tcp_client = -1;
            }
        }
    }
    xSemaphoreGive(s_client_mu);
}

static void emit_frame(uint8_t node_id, const csi_frame_t *frame)
{
    static char line[CSI_LINE_MAX];
    if (csi_format_csv(line, sizeof(line), node_id, frame) > 0) {
        emit_line(line);
    }
}

static TickType_t rate_gap_ticks(void)
{
    const int hz = CSI_REMOTE_HZ > 0 ? CSI_REMOTE_HZ : 20;
    return pdMS_TO_TICKS(1000 / hz);
}

static void agg_local_csi_task(void *arg)
{
    (void)arg;
    const TickType_t min_gap = rate_gap_ticks();
    TickType_t last_emit = 0;
    uint8_t idx = 0;
    while (true) {
        if (xQueueReceive(csi_ready_queue(), &idx, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        const TickType_t now = xTaskGetTickCount();
        if ((now - last_emit) < min_gap) {
            csi_release_frame(idx);
            continue;
        }
        csi_frame_t *frame = csi_frame_at(idx);
        if (frame) {
            emit_frame((uint8_t)CSI_NODE_ID, frame);
            last_emit = now;
        }
        csi_release_frame(idx);
    }
}

#define AGG_LINK_SLOTS 8

static void agg_udp_rx_task(void *arg)
{
    (void)arg;
    const int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock < 0) {
        ESP_LOGE(TAG, "UDP socket failed");
        vTaskDelete(NULL);
        return;
    }

    int yes = 1;
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    struct sockaddr_in addr = {};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(CSI_UDP_PORT);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        ESP_LOGE(TAG, "UDP bind failed on %u", (unsigned)CSI_UDP_PORT);
        close(sock);
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "Aggregator UDP :%u (remotes)  TCP :%u (laptop)",
             (unsigned)CSI_UDP_PORT, (unsigned)CSI_TCP_PORT);

    uint8_t seen_nodes[AGG_LINK_SLOTS] = {};
    TickType_t seen_at[AGG_LINK_SLOTS] = {};
    int seen_n = 0;
    TickType_t last_link_report = 0;

    uint8_t pkt[CSI_UDP_MAX];
    csi_frame_t frame;
    while (true) {
        struct sockaddr_in from = {};
        socklen_t fromlen = sizeof(from);
        const int n = recvfrom(sock, pkt, sizeof(pkt), 0, (struct sockaddr *)&from, &fromlen);
        if (n <= 0) {
            continue;
        }
        uint8_t node_id = 0;
        if (!csi_udp_to_frame(pkt, (size_t)n, &frame, &node_id)) {
            continue;
        }
        emit_frame(node_id, &frame);

        const TickType_t now = xTaskGetTickCount();
        int slot = -1;
        for (int i = 0; i < seen_n; i++) {
            if (seen_nodes[i] == node_id) {
                slot = i;
                break;
            }
        }
        if (slot < 0 && seen_n < AGG_LINK_SLOTS) {
            slot = seen_n++;
            seen_nodes[slot] = node_id;
            char note[64];
            snprintf(note, sizeof(note), "# CSI_LINK,up,%u", (unsigned)node_id);
            emit_line(note);
            ESP_LOGI(TAG, "Remote node %u online", (unsigned)node_id);
        }
        if (slot >= 0) {
            seen_at[slot] = now;
        }

        if ((now - last_link_report) > pdMS_TO_TICKS(5000)) {
            last_link_report = now;
            for (int i = 0; i < seen_n; i++) {
                const uint32_t age_ms = (uint32_t)((now - seen_at[i]) * portTICK_PERIOD_MS);
                char note[80];
                snprintf(note, sizeof(note), "# CSI_LINK,age,%u,%lu",
                         (unsigned)seen_nodes[i], (unsigned long)age_ms);
                emit_line(note);
                if (age_ms > 3000) {
                    ESP_LOGW(TAG, "Remote node %u quiet for %lums",
                             (unsigned)seen_nodes[i], (unsigned long)age_ms);
                }
            }
        }
    }
}

static void agg_tcp_server_task(void *arg)
{
    (void)arg;
    const int listen_fd = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    if (listen_fd < 0) {
        ESP_LOGE(TAG, "TCP listen socket failed");
        vTaskDelete(NULL);
        return;
    }

    int yes = 1;
    setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

    struct sockaddr_in addr = {};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(CSI_TCP_PORT);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        ESP_LOGE(TAG, "TCP bind failed on %u", (unsigned)CSI_TCP_PORT);
        close(listen_fd);
        vTaskDelete(NULL);
        return;
    }
    if (listen(listen_fd, 1) < 0) {
        ESP_LOGE(TAG, "TCP listen failed");
        close(listen_fd);
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "Laptop TCP stream ready on 192.168.4.1:%u", (unsigned)CSI_TCP_PORT);

    while (true) {
        struct sockaddr_in client = {};
        socklen_t clen = sizeof(client);
        const int fd = accept(listen_fd, (struct sockaddr *)&client, &clen);
        if (fd < 0) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        ESP_LOGI(TAG, "Laptop connected over TCP");
        int yes_tcp = 1;
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &yes_tcp, sizeof(yes_tcp));
        tcp_set_client(fd);
    }
}

void agg_start_tasks(void)
{
    s_client_mu = xSemaphoreCreateMutex();
    if (s_client_mu == NULL) {
        ESP_LOGE(TAG, "mutex alloc failed");
        abort();
    }

    print_banner("agg");
    BaseType_t ok1 = xTaskCreatePinnedToCore(agg_local_csi_task, "csi_local", 6144, NULL, 5, NULL, 0);
    BaseType_t ok2 = xTaskCreatePinnedToCore(agg_udp_rx_task, "csi_udp_rx", 6144, NULL, 5, NULL, 1);
    BaseType_t ok3 = xTaskCreatePinnedToCore(agg_tcp_server_task, "csi_tcp", 4096, NULL, 4, NULL, 1);
    if (ok1 != pdPASS || ok2 != pdPASS || ok3 != pdPASS) {
        ESP_LOGE(TAG, "Failed to start aggregator tasks");
        abort();
    }
}

static void remote_udp_tx_task(void *arg)
{
    (void)arg;
    const TickType_t min_gap = rate_gap_ticks();
    TickType_t last_send = 0;

    const int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    if (sock < 0) {
        ESP_LOGE(TAG, "UDP socket failed");
        vTaskDelete(NULL);
        return;
    }

    struct sockaddr_in dest = {};
    dest.sin_family = AF_INET;
    dest.sin_port = htons(CSI_UDP_PORT);
    dest.sin_addr.s_addr = inet_addr("192.168.4.1");

    ESP_LOGI(TAG, "Remote node %u → UDP 192.168.4.1:%u @ %d Hz",
             (unsigned)CSI_NODE_ID, (unsigned)CSI_UDP_PORT, CSI_REMOTE_HZ);

    uint8_t pkt[CSI_UDP_MAX];
    uint8_t idx = 0;
    uint32_t sent = 0;
    uint32_t skipped = 0;

    while (true) {
        if (xQueueReceive(csi_ready_queue(), &idx, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        const TickType_t now = xTaskGetTickCount();
        if ((now - last_send) < min_gap) {
            skipped++;
            csi_release_frame(idx);
            continue;
        }

        csi_frame_t *frame = csi_frame_at(idx);
        if (frame) {
            const int n = csi_frame_to_udp(pkt, sizeof(pkt), (uint8_t)CSI_NODE_ID, frame);
            if (n > 0) {
                const int w = sendto(sock, pkt, n, 0, (struct sockaddr *)&dest, sizeof(dest));
                if (w == n) {
                    sent++;
                    last_send = now;
                }
            }
        }
        csi_release_frame(idx);

        if ((sent + skipped) % 200 == 0) {
            ESP_LOGI(TAG, "udp_sent=%lu skipped=%lu drops=%lu",
                     (unsigned long)sent, (unsigned long)skipped,
                     (unsigned long)csi_drop_count());
        }
    }
}

void remote_start_tasks(void)
{
    print_banner("remote");
    BaseType_t ok = xTaskCreatePinnedToCore(remote_udp_tx_task, "csi_udp_tx", 6144, NULL, 5, NULL, 0);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "Failed to start remote UDP task");
        abort();
    }
}

static void legacy_serial_task(void *arg)
{
    (void)arg;
    print_banner("legacy");
    uint8_t idx = 0;
    while (true) {
        if (xQueueReceive(csi_ready_queue(), &idx, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        csi_frame_t *frame = csi_frame_at(idx);
        if (frame) {
            emit_frame((uint8_t)CSI_NODE_ID, frame);
        }
        csi_release_frame(idx);
    }
}

void legacy_start_serial_task(void)
{
    BaseType_t ok = xTaskCreatePinnedToCore(legacy_serial_task, "csi_serial", 6144, NULL, 5, NULL, 0);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "Failed to start legacy serial task");
        abort();
    }
}
