/*
 * scope_bridge — ESP32-S3 transparent USB-CDC <-> WiFi TCP bridge for the
 * OpenScope-2C53T debug shell (and any other CDC-ACM device).
 *
 *   scope USB-C  <->  ESP32-S3 native USB (host mode, GPIO19/20, VBUS supplied)
 *   ESP32-S3 WiFi (station)  ->  TCP server on CONFIG_BRIDGE_TCP_PORT
 *
 * Every byte the scope sends is forwarded to all connected TCP clients; every
 * byte a client sends is written to the scope. No framing, no parsing: the
 * host tools (host/t_scope.py --tcp) speak the shell protocol end to end, so
 * the bridge never needs updating when the shell grows.
 *
 * Built on ESP-IDF usb_host + the espressif/usb_host_cdc_acm component.
 */
#include <string.h>
#include <errno.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "usb/usb_host.h"
#include "usb/cdc_acm_host.h"
#include "lwip/sockets.h"

static const char *TAG = "bridge";

#define MAX_CLIENTS 4
static int clients[MAX_CLIENTS];
static SemaphoreHandle_t clients_lock;
static cdc_acm_dev_hdl_t cdc_dev = NULL;
static SemaphoreHandle_t cdc_lock;
static EventGroupHandle_t wifi_events;
#define WIFI_CONNECTED_BIT BIT0

/* ---------------- TCP fan-out ---------------- */
static void clients_broadcast(const uint8_t *data, size_t len)
{
    xSemaphoreTake(clients_lock, portMAX_DELAY);
    for (int i = 0; i < MAX_CLIENTS; i++) {
        if (clients[i] < 0) continue;
        if (send(clients[i], data, len, MSG_DONTWAIT) < 0 && errno != EAGAIN) {
            close(clients[i]); clients[i] = -1;
        }
    }
    xSemaphoreGive(clients_lock);
}

/* ---------------- USB CDC host ---------------- */
static bool cdc_rx_cb(const uint8_t *data, size_t len, void *arg)
{
    clients_broadcast(data, len);
    return true;
}

static void cdc_event_cb(const cdc_acm_host_dev_event_data_t *ev, void *user_ctx)
{
    switch (ev->type) {
    case CDC_ACM_HOST_ERROR:
        ESP_LOGW(TAG, "CDC error %d", ev->data.error); break;
    case CDC_ACM_HOST_DEVICE_DISCONNECTED:
        ESP_LOGI(TAG, "scope disconnected");
        xSemaphoreTake(cdc_lock, portMAX_DELAY);
        cdc_acm_host_close(ev->data.cdc_hdl); cdc_dev = NULL;
        xSemaphoreGive(cdc_lock);
        break;
    default: break;
    }
}

static void usb_lib_task(void *arg)
{
    const usb_host_config_t cfg = { .skip_phy_setup = false, .intr_flags = ESP_INTR_FLAG_LEVEL1 };
    ESP_ERROR_CHECK(usb_host_install(&cfg));
    xTaskNotifyGive((TaskHandle_t)arg);
    for (;;) {
        uint32_t flags;
        usb_host_lib_handle_events(portMAX_DELAY, &flags);
        if (flags & USB_HOST_LIB_EVENT_FLAGS_NO_CLIENTS) usb_host_device_free_all();
    }
}

/* Keep trying to open a CDC-ACM device; reopen after a disconnect. */
static void cdc_open_task(void *arg)
{
    const cdc_acm_host_device_config_t dev_cfg = {
        .connection_timeout_ms = 5000, .out_buffer_size = 512, .in_buffer_size = 2048,
        .event_cb = cdc_event_cb, .data_cb = cdc_rx_cb, .user_arg = NULL,
    };
    for (;;) {
        if (cdc_dev == NULL) {
            cdc_acm_dev_hdl_t h = NULL;
            uint16_t vid = CONFIG_BRIDGE_USB_VID, pid = CONFIG_BRIDGE_USB_PID;
            esp_err_t err = cdc_acm_host_open(vid ? vid : CDC_HOST_ANY_VID, pid ? pid : CDC_HOST_ANY_PID,
                                              0, &dev_cfg, &h);
            if (err == ESP_OK) {
                cdc_acm_line_coding_t lc = { .dwDTERate = 115200, .bCharFormat = 0, .bParityType = 0, .bDataBits = 8 };
                cdc_acm_host_line_coding_set(h, &lc);
                cdc_acm_host_set_control_line_state(h, true, false);   /* DTR on: some shells wait for it */
                xSemaphoreTake(cdc_lock, portMAX_DELAY); cdc_dev = h; xSemaphoreGive(cdc_lock);
                ESP_LOGI(TAG, "scope CDC opened");
            }
        }
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

static void cdc_write(const uint8_t *data, size_t len)
{
    xSemaphoreTake(cdc_lock, portMAX_DELAY);
    if (cdc_dev) cdc_acm_host_data_tx_blocking(cdc_dev, data, len, 1000);
    xSemaphoreGive(cdc_lock);
}

/* ---------------- TCP server ---------------- */
static void client_task(void *arg)
{
    int fd = (int)(intptr_t)arg;
    uint8_t buf[256];
    for (;;) {
        int n = recv(fd, buf, sizeof(buf), 0);
        if (n <= 0) break;
        cdc_write(buf, (size_t)n);
    }
    xSemaphoreTake(clients_lock, portMAX_DELAY);
    for (int i = 0; i < MAX_CLIENTS; i++) if (clients[i] == fd) clients[i] = -1;
    xSemaphoreGive(clients_lock);
    close(fd);
    vTaskDelete(NULL);
}

static void tcp_server_task(void *arg)
{
    int lfd = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1; setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = htons(CONFIG_BRIDGE_TCP_PORT), .sin_addr.s_addr = htonl(INADDR_ANY) };
    bind(lfd, (struct sockaddr *)&a, sizeof(a)); listen(lfd, 2);
    ESP_LOGI(TAG, "TCP bridge listening on %d", CONFIG_BRIDGE_TCP_PORT);
    for (;;) {
        int fd = accept(lfd, NULL, NULL);
        if (fd < 0) continue;
        int slot = -1;
        xSemaphoreTake(clients_lock, portMAX_DELAY);
        for (int i = 0; i < MAX_CLIENTS; i++) if (clients[i] < 0) { clients[i] = fd; slot = i; break; }
        xSemaphoreGive(clients_lock);
        if (slot < 0) { close(fd); continue; }
        int nd = 1; setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &nd, sizeof(nd));
        xTaskCreate(client_task, "client", 3072, (void *)(intptr_t)fd, 5, NULL);
    }
}

/* ---------------- WiFi station ---------------- */
static void wifi_evt(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) esp_wifi_connect();
    else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) { xEventGroupClearBits(wifi_events, WIFI_CONNECTED_BIT); vTaskDelay(pdMS_TO_TICKS(1000)); esp_wifi_connect(); }
    else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data; ESP_LOGI(TAG, "IP " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void wifi_start(void)
{
    ESP_ERROR_CHECK(esp_netif_init()); ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT(); ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_evt, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_evt, NULL));
    wifi_config_t wc = { 0 };
    strncpy((char *)wc.sta.ssid, CONFIG_BRIDGE_WIFI_SSID, sizeof(wc.sta.ssid));
    strncpy((char *)wc.sta.password, CONFIG_BRIDGE_WIFI_PASSWORD, sizeof(wc.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA)); ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
}

void app_main(void)
{
    esp_err_t r = nvs_flash_init();
    if (r == ESP_ERR_NVS_NO_FREE_PAGES || r == ESP_ERR_NVS_NEW_VERSION_FOUND) { nvs_flash_erase(); nvs_flash_init(); }
    clients_lock = xSemaphoreCreateMutex(); cdc_lock = xSemaphoreCreateMutex(); wifi_events = xEventGroupCreate();
    for (int i = 0; i < MAX_CLIENTS; i++) clients[i] = -1;

    wifi_start();

    TaskHandle_t me = xTaskGetCurrentTaskHandle();
    xTaskCreatePinnedToCore(usb_lib_task, "usb_lib", 4096, me, 10, NULL, 0);
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);              /* usb_host_install done */
    ESP_ERROR_CHECK(cdc_acm_host_install(NULL));
    xTaskCreate(cdc_open_task, "cdc_open", 4096, NULL, 6, NULL);
    xTaskCreate(tcp_server_task, "tcp_srv", 4096, NULL, 5, NULL);
    ESP_LOGI(TAG, "bridge up");
}
